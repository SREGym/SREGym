"""The submitted diagnosis, built by code from the investigation's choices and the collected facts. No model writes
text.

Lines, in this order (Root cause and Fault type always, the others when they have something to say):
- Root cause: one subject and its faulty setting as field = value, with the previous or expected value when known.
  The subject is the object that holds the setting (subject_of): a referenced object that does not exist, marked
  missing, with the workload that references it; the recurring workload (a CronJob) that reads a changed config object
  and applies it; a policy or cluster object with the workload it acts on; one change applied to several workloads,
  named together; else the concluded workload. A log line, an event, file content, JSON, encoded data or a timing is
  never the stated fact: when the investigation's key evidence is a symptom, the fact is the setting Kubernetes
  consults to produce that symptom (RESPONSES).
- Mechanism: what Kubernetes does with that setting, then the symptom as the platform reported it; without such a
  response, the component's own causal finding and at most one log quote, taken only when every pod runs one pod
  template (an older revision's errors cannot show what a new setting does).
- Fault type: the class of the faulty object or field, else the investigation's category, with the specific value.
- Changed: when each object of the cause was written, by whom when known, how long before the first symptom, and
  every changed field as old -> new.
- Impact: the subject's own state, then the failing components the cause accounts for (tree.explained_by).
- Scope: what the subject applies to beyond the workload it broke.
- Contributing: the other party (a webhook's backend, a role's service account, an autoscaler, the other end of a
  timing-out call path, the clients presenting rejected logins, a priority class that preempts, clients naming a
  missing Service).
- Evidence: at most MAX_EVIDENCE labeled facts not stated above.
A block is at most MAX_TOTAL characters (Evidence, then Impact, give way first). A second, independent cause gets its
own block.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass, field

from clients.jev_diag.timeutil import parse_time, seconds_between
from clients.jev_diag.v1_1 import checks
from clients.jev_diag.v1_1.checks import parse_cpu_millis
from clients.jev_diag.v1_1.classifier import OTHER_OPTION, Diagnosis
from clients.jev_diag.v1_1.collector import ClusterSnapshot, is_error_line
from clients.jev_diag.v1_1.derive import TELEMETRY_RE, diagnostic_signals, object_key

MAX_TOTAL = 1600  # characters of one cause's block
MAX_ROOT = 300  # characters of the Root cause line
MAX_EVIDENCE = 5
MAX_IMPACT = 5
MAX_CHANGED = 400
VALUE_CHARS = 160  # a setting longer than this is file content or a script, not a value to state
CPU_SATURATED = 0.9  # near the CPU limit; usage alone is not a measurement of throttling
QUOTE_CHARS = 160
# An evidence item with at least this share of the key item's support is stated next to it.
COMPARABLE_SUPPORT = 0.5
SAME_CHANGE_SECONDS = 10  # one change applied to several workloads lands within this window
CONFIG_KINDS = ("ConfigMap", "Secret")
# Kubernetes defaults of probe timings, stated next to a value that differs from them.
PROBE_DEFAULTS = {"periodSeconds": 10, "timeoutSeconds": 1, "failureThreshold": 3}
_UNTRUSTED = re.compile(r"x509|certificate is not trusted|tls: failed to verify")  # a webhook call failing TLS checks


@dataclass(frozen=True)
class Response:
    """What Kubernetes does when a setting meets its condition. `symptom` matches the platform's own reason or message
    (scheduler predicate, kubelet, container runtime, API server); `setting` names the setting it consults (looked up
    by governing_setting); `kind` is the fault type that setting belongs to."""

    symptom: re.Pattern
    setting: str
    kind: str
    does: str


def _response(symptom: str, setting: str, kind: str, does: str) -> Response:
    return Response(re.compile(symptom), setting, kind, does)


RESPONSES = (
    _response(
        r"OOMKilled",
        "memory_limit",
        "resource_limits",
        "the kernel kills a container whose memory use reaches its limit (OOMKilled) and the kubelet restarts it",
    ),
    _response(
        r"ErrImagePull|ImagePullBackOff|Failed to pull image",
        "image",
        "image_or_binary",
        "the kubelet cannot pull the image, so the container never starts",
    ),
    _response(
        r"executable file not found|exec format error",
        "image",
        "image_or_binary",
        "the container runtime cannot start the container's command from this image",
    ),
    _response(
        r"didn't have free ports",
        "host_port",
        "scheduling_constraint",
        "a host port can be bound by one pod per node, so the scheduler skips every node where it is taken",
    ),
    _response(
        r"didn't match pod anti-affinity",
        "anti_affinity",
        "scheduling_constraint",
        "required pod anti-affinity allows one matching pod per node, so replicas beyond the eligible nodes stay "
        "Pending",
    ),
    _response(
        r"volume node affinity conflict",
        "node_selector",
        "scheduling_constraint",
        "a pod can run only on nodes its PersistentVolume's node affinity allows, so a node selector pointing "
        "elsewhere leaves it Pending",
    ),
    _response(
        r"didn't match Pod's node affinity/selector",
        "node_selector",
        "scheduling_constraint",
        "the scheduler places a pod only on nodes whose labels match its node selector and node affinity",
    ),
    _response(  # every cluster's control plane carries this taint; it explains nothing
        r"untolerated taint \{(?!node-role\.kubernetes\.io/control-plane)",
        "taint",
        "scheduling_constraint",
        "the scheduler places a pod only on nodes whose NoSchedule taints it tolerates",
    ),
    _response(
        r"Insufficient (?:cpu|memory)",
        "requests",
        "resource_limits",
        "the scheduler places a pod only on a node with that much unrequested capacity",
    ),
    _response(
        r'persistentvolumeclaim "[^"]+" not found',
        "missing",
        "missing_object",
        "a pod whose PersistentVolumeClaim does not exist cannot be scheduled",
    ),
    _response(
        r'(?:configmap|secret) "[^"]+" not found',
        "missing",
        "missing_object",
        "the kubelet cannot mount or read an object that does not exist, so the pod's containers never start",
    ),
    _response(
        r"Liveness probe failed",
        "liveness",
        "health_probe_config",
        "the kubelet restarts a container after failureThreshold consecutive failed liveness probes",
    ),
    _response(
        r"Readiness probe failed",
        "readiness",
        "health_probe_config",
        "a container that fails its readiness probe is taken out of its Services' endpoints",
    ),
    _response(
        r"FailedCreatePodSandBox|failed to set ?up network for sandbox",
        "network",
        "pod_network_or_ipam",
        "the kubelet has the network plugin give every new pod a network address before its containers start; when "
        "the plugin or its address management fails, the pod's sandbox is never created and its containers never "
        "start",
    ),
    _response(
        r"Preempted by",
        "priority",
        "preemption_or_priority",
        "the scheduler evicts lower-priority pods to place a pod of higher priority",
    ),
    _response(
        _UNTRUSTED.pattern,
        "webhook",
        "certificate_or_tls",
        "the API server must verify a webhook's serving certificate against its clientConfig.caBundle; when it "
        "cannot, the call fails and failurePolicy=Fail rejects the request",
    ),
    _response(
        r"context deadline exceeded|Timeout: request did not complete",
        "webhook",
        "admission_or_namespace_policy",
        "the API server calls every matching admission webhook in turn and fails the request once its own deadline "
        "passes",
    ),
    _response(
        r"exceeded quota|must specify",
        "quota",
        "admission_or_namespace_policy",
        "the API server rejects a pod that does not fit the namespace's ResourceQuota",
    ),
    _response(
        r"missing request for",
        "hpa",
        "resource_limits",
        "an autoscaler computes CPU utilization relative to the containers' CPU requests, so without a request it "
        "cannot scale",
    ),
)

# The fault type an object kind implies when that object is the subject.
KIND_TYPES = {
    "MutatingWebhookConfiguration": "admission_or_namespace_policy",
    "ValidatingWebhookConfiguration": "admission_or_namespace_policy",
    "ResourceQuota": "admission_or_namespace_policy",
    "LimitRange": "admission_or_namespace_policy",
    "NetworkPolicy": "network_policy",
    "Role": "rbac_permission",
    "ClusterRole": "rbac_permission",
    "RoleBinding": "rbac_permission",
    "ClusterRoleBinding": "rbac_permission",
    "PriorityClass": "preemption_or_priority",
}
# The fault type of a platform layer failing for several workloads (derive.platform_findings).
LAYER_TYPES = {
    "pod_network": "pod_network_or_ipam",
    "pod_security": "admission_or_namespace_policy",
    "preemption": "preemption_or_priority",
    "node_pressure": "node_or_kubelet",
}
# The fault type of a faulty field, by Kubernetes field names (first match wins; config_value is the generic class).
FIELD_TYPES = (
    ("missing_object", r"\(missing\)|does not exist|no Service selects"),
    ("certificate_or_tls", r"certificate|caBundle|\bTLS\b|\btls\b|x509"),
    ("image_or_binary", r"\bimage\b"),
    ("health_probe_config", r"\bprobe\b"),
    ("scheduling_constraint", r"nodeSelector|node_selector|affinity|toleration|taint|hostPort|host port"),
    ("resource_limits", r"\b(?:limits|requests)\.(?:cpu|memory)|\bresources\b"),
    ("rollout_or_replica_config", r"\breplicas\b|maxUnavailable|maxSurge|\bstrategy\b"),
    ("dns_resolution", r"dnsPolicy|dnsConfig|hostAliases|Corefile|name resolution|NXDOMAIN"),
    ("traffic_policy", r"(?:internal|external)TrafficPolicy|sessionAffinity"),
    ("service_routing", r"\bselector\b|targetPort"),
    ("storage_volume", r"PersistentVolume|\bPVC\b|accessModes|ReadWriteOnce"),
    ("preemption_or_priority", r"[Pp]riority[Cc]lass"),
    ("config_value", r"\benv\b|\bargs\b|\bcommand\b|ConfigMap|Secret"),
)
# What Kubernetes does with a setting itself, for faults no platform event reports (pattern over the stated fact).
SETTING_RESPONSES = (
    (
        r"dnsPolicy\W+(?:\S+ -> )?None",
        "with dnsPolicy None a pod resolves names only through its dnsConfig nameservers, so cluster Service names "
        "resolve only if those nameservers serve them",
    ),
    (r"hostAliases", "hostAliases entries are written to the pod's /etc/hosts and answer those names before DNS"),
    (
        r"internalTrafficPolicy\W+Local",
        "kube-proxy sends a client's traffic only to endpoints on the client's own node",
    ),
    (r"externalTrafficPolicy\W+Local", "external traffic reaches only nodes that run an endpoint of the Service"),
    (
        r"maxUnavailable|maxSurge",
        "a rolling update stops old pods and starts new ones within maxUnavailable and maxSurge; when every old pod may "
        "stop before a new one is ready, the workload is down until the new pods become ready",
    ),
    (
        r"replicas = 0|desired=0|zero replicas",
        "a workload with 0 replicas runs no pods, so its Services have no endpoints",
    ),
    (r"defines env var \S+ \d+ times", "when a container defines an env var more than once, the last definition wins"),
    (
        r"podSelector",
        "pods a NetworkPolicy selects accept only the traffic its rules allow; a listed policy type "
        "without rules drops all traffic of that type",
    ),
    (r"\bselector\b", "a Service routes only to ready pods whose labels match its selector"),
    (
        r"Corefile|overrides name resolution",
        "pods resolve cluster names through CoreDNS, which answers a name matching a rule from that rule instead of "
        "from the cluster's Services",
        r"rcode \w+",
    ),
    (
        r"certificate .*expired",
        "clients that verify the certificate served for that host reject the TLS handshake once it has expired",
    ),
    (
        r"\btaints?\b",
        "the scheduler places a pod only on nodes whose NoSchedule taints it tolerates (NoExecute also "
        "evicts running pods)",
    ),
    (r"^rules: ", "the API server denies a service account every request its bound roles do not grant (403 Forbidden)"),
    (
        r"at admission",
        "the API server applies a mutating webhook's patch to every matching pod when it is created, so "
        "running pods differ from their template",
    ),
    (r"listen on port \d+", "a pod's containers share one network namespace, so only one of them can bind a port"),
    (r"initContainers|init container", "a pod's main containers start only after every init container has exited"),
)

# Evidence items and signals that report what happened rather than a setting (the item formats of
# tree.detail_evidence and the collector's state signals): they explain a cause but are never its stated fact.
_SYMPTOM = re.compile(
    r"^(?:[a-z]+/\S+: )?(?:log x\d+|previous log |repeated failure |event \S+ x\d+ |\d+ error log lines|"
    r"still-occurring errors|this component's still-occurring|dependency \S+ (?:is|looks)|reachability |"
    r"errors logged by|warning events?:|logs report |pod [a-z0-9.-]+-[a-z0-9]{5}: |only \d+ of \d+ desired|"
    r"service \S+ selects this component|job has |\d+ jobs? |\d+ pod\(s\) |HPA |no pods exist|"
    r"container \S+ (?:cpu|memory) usage|selected by NetworkPolicy|server rejects logins|rejects logins|"
    r"pods of this workload were preempted)"
    r"|\brestarted \d+ times?\b|CrashLoopBackOff|was OOMKilled|\bis (?:not ready|Pending)\b|has not completed"
)
# A trend line: the same symptom counted again over the sampling window (zero seconds in a replay).
_TREND = re.compile(r"more time\(s\) in the last \d+s|after another \d+s")
# A signal that restates a write the Changed line already states.
_WRITE = re.compile(
    r"^(?:spec modified \d+s ago|pod template changed \d+s ago|\S+ \S+ was (?:created|modified) \d+s ago)"
)
# Parts of a fact that carry only a time or a write (the Changed line states those).
_TIMING = re.compile(
    r"^(?:\d+s (?:before|after) the first active symptom|written by .+ after the application was deployed|"
    r"(?:spec )?modified \d+s ago.*|pod template changed \d+s ago|created \d+s ago.*|.+ was (?:created|modified) "
    r"\d+s ago.*)$"
)
_FRAMING = re.compile(r"^(?:pod template changed in revision \d+: |pod template changes: |now )")
# Encoded data: a base64 run mixes upper and lower case and digits (an image digest is lower-case hex).
_ENCODED = re.compile(
    r"base64|characters of encoded data|(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*\d)"
    r"[A-Za-z0-9+/]{80,}"
)
_CONFIG_KEY = re.compile(r"^(ConfigMap|Secret) (\S+) key (\S+): (.*)$", re.S)
_GENERIC_CONTAINER = re.compile(r"^container (\S+) image (\S+) resources (.*)$")
_SETTING_KEY = re.compile(
    r"^(container \S+ (?:env \S+?|image|limits\.\w+|requests\.\w+|command|args|\w+ probe)|pod \w+)\b"
)
_STATE = re.compile(r"^(\S+): (?:last )?(?:waiting|terminated) (\w+)")
_BECAUSE = re.compile(r"(?i)\bso\b|because")  # a finding that states its own cause
_CAUSAL = re.compile(r"(?i)cannot|never|until it")
_STACK_FRAME = re.compile(r"\(\*?\w+\)\.\w|^\s*at [\w.$]+\(|\.(?:go|py|java|js|rb):\d+\)?$|^goroutine \d+|^File \"")
_SEVERITY = (
    re.compile(r"(?i)\b(?:fatal|panic|critical|emerg)|OutOfMemory|out of memory"),
    re.compile(r"(?i)error|exception|\bfail"),
)
_NAME_PREFIX = re.compile(r"^(?:\[pod/[^\]]+\]\s?)?[a-z0-9][a-z0-9./-]{0,58}: ")
_TIMESTAMP = re.compile(r"^\[?\S*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s\]]*\]?\s*")
_JSON_MSG = re.compile(r'"msg"\s*:\s*"(?P<msg>[^"]+)"')
_JSON_ERR = re.compile(r'"(?:error|errmsg|reason|codeName)"\s*:\s*"(?P<err>[^"]+)"')
_CONTAINER = re.compile(r"\bcontainer \S+ ")


def describe(kind: str, name: str, namespace: str | None) -> str:
    return f"{kind} `{name}`" + (f" in namespace `{namespace}`" if namespace else "")


def component_phrase(comp: dict) -> str:
    return describe(comp["kind"], comp["name"], comp["namespace"])


def short_name(snapshot: ClusterSnapshot, cid: str) -> str:
    comp = snapshot.components.get(cid) or {}
    return f"{comp['kind']} `{comp['name']}`" if comp else cid


def cut(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word boundary with an ellipsis."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    return (head.rsplit(" ", 1)[0] if " " in head[limit // 2 :] else head).rstrip(" ,;:") + "…"


def shorten(fact: str, limit: int) -> str:
    """A fact within `limit`: a change keeps its new value, any other statement keeps both ends (a compound finding
    states its conclusion last)."""
    return cut_change(fact, limit) if " -> " in fact.partition("; ")[0] else cut_middle(fact, limit)


def cut_change(text: str, limit: int) -> str:
    """`cut`, except that a leading change (field: old -> new) gives up its old value before its new one."""
    first, _, rest = text.partition("; ")
    m = re.match(r"^(.*?: )(.*) -> (.*)$", first)
    if len(text) <= limit or not m:
        return cut(text, limit)
    head, old, new = m.groups()
    if len(head) + len(new) + 4 > limit:
        before, after = old.split(), new.split()
        changes = [
            op for op in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if op[0] != "equal"
        ]
        if changes:
            old_excerpt = " … ".join(
                " ".join(before[max(0, i - 1) : min(len(before), j + 1)]) for _, i, j, _, _ in changes
            )
            new_excerpt = " … ".join(
                " ".join(after[max(0, i - 1) : min(len(after), j + 1)]) for _, _, _, i, j in changes
            )
            excerpt = f"{head}… {old_excerpt} -> … {new_excerpt}"
            if len(excerpt) < len(text):
                return cut(excerpt, limit)
    room = limit - len(head) - len(new) - 4 - (len(rest) + 2 if rest else 0)
    return cut(f"{head}{cut(old, room) if room >= 12 else '…'} -> {new}" + (f"; {rest}" if rest else ""), limit)


# --------------------------------------------------------------------------- values


def flat(value, path: str = "") -> list[str]:
    """A JSON value as path=value pairs (strings that hold JSON are opened too; taints and tolerations in Kubernetes'
    key=value:effect notation)."""
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return flat(json.loads(value), path)
        except ValueError:
            pass
    if isinstance(value, dict):
        if "key" in value and "effect" in value:
            taint = f"{value['key']}" + (f"={value['value']}" if value.get("value") else "") + f":{value['effect']}"
            return [f"{path}={taint}" if path else taint]
        return [x for k, v in value.items() for x in flat(v, f"{path}.{k}" if path else str(k))]
    if isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            joined = ", ".join(map(str, value))
            return ([f"{path}={joined}"] if path else [joined]) if value else []
        return [x for v in value for x in flat(v, path)]
    if value is None or value == "":
        return []
    return [f"{path}={value}" if path else str(value)]


def readable(text: str) -> str | None:
    """`text` with each JSON or Python-literal document written as path=value pairs. A document the collector cut off
    keeps the key-value pairs it still holds whole, followed by an ellipsis; None when it holds none."""
    out, i = [], 0
    while i < len(text):
        if text[i] in "{[" and (i + 1 < len(text) and text[i + 1] in "\"'{[]}"):
            end = _closing(text, i)
            if end is None:
                whole = dict(re.findall(r'"([\w./-]+)"\s*:\s*"?([^"{}\[\],…]+?)"?\s*[,}]', text[i:]))
                return "".join(out) + ", ".join(flat(whole)) + " …" if whole else None
            raw = text[i : end + 1]
            try:
                value = json.loads(raw)
            except ValueError:
                try:
                    value = ast.literal_eval(raw)
                except (ValueError, SyntaxError):
                    value = raw
            out.append(", ".join(flat(value)) if not isinstance(value, str) else value)
            i = end + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _closing(text: str, start: int) -> int | None:
    depth, quote_char, escaped = 0, None, False
    for i in range(start, len(text)):
        ch = text[i]
        if quote_char:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                quote_char = None
        elif ch in "\"'":
            quote_char = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i
    return None


def clean(text: str | None) -> str | None:
    """A fact as plain values: framing and timing-only parts dropped, JSON as path=value pairs, line breaks folded;
    None when nothing value-bearing is left (encoded data, a timing, a cut-off document)."""
    parts = []
    pieces = re.split(r"; (?=\S)", text or "")
    while pieces:
        part = _FRAMING.sub("", pieces.pop(0).strip())
        held = re.match(r"^it (holds|runs) (.*)$", part, re.S)
        if held:  # an object's content (it may hold "; "), stated only when it reads as a few values
            content = [held.group(2)]
            while pieces and not _TIMING.match(pieces[0].strip()):
                content.append(pieces.pop(0))
            values = readable("; ".join(content))
            if values and len(values) <= VALUE_CHARS and not _ENCODED.search(values):
                parts.append(f"it {held.group(1)} {values}")
            continue
        if not part or _TIMING.match(part) or _TREND.search(part) or _ENCODED.search(part):
            continue
        part = readable(part)
        if part:
            parts.append(old_new(" ".join(part.replace("⏎", " ").split())))
    return "; ".join(dict.fromkeys(p for p in parts if p)) or None


def old_new(change: str) -> str:
    """A pod template change as field: old -> new (an added setting was unset before, a removed one is unset now)."""
    added = re.match(r"^container (\S+) added \((\w+), (.*)\)$", change)
    if added:
        return f"{added.group(2)} {added.group(1)}: none -> {added.group(3)}"
    change = re.sub(r"^(.+?) added: (.*)$", r"\1: unset -> \2", change)
    return re.sub(r"^(.+?) removed \(was (.*)\)$", r"\1: \2 -> unset", change)


def is_symptom(text: str) -> bool:
    return bool(_SYMPTOM.search(text))


def value_text(item: str | None, d: Diagnosis, comp: dict) -> str | None:
    """`item` as the stated value of a setting, or None when it is not one: a symptom, file content longer than a
    value, or nothing left after cleaning. A setting the pod template changed is stated as its change (old -> new)."""
    if not item or is_symptom(item):
        return None
    config = _CONFIG_KEY.match(item)
    if config:
        content = " ".join(config.group(4).replace("⏎", " ").split())
        if len(content) > VALUE_CHARS or _ENCODED.search(content):
            return None  # a file, not a value
        return clean(f"{config.group(1)} {config.group(2)} key {config.group(3)} = {content}")
    generic = _GENERIC_CONTAINER.match(item)
    if generic:  # one item holding both the image and the resources: the one the decision is about
        name, image, resources = generic.groups()
        category = d.category_result.choice if d.category_result else None
        oom = any("OOMKilled" in s for p in comp.get("pods") or [] for s in p.get("container_states") or [])
        if category == "resource_limits" or oom:
            typed = next((c for c in comp.get("containers") or [] if c.get("name") == name), {})
            memory = (typed.get("limits") or {}).get("memory")
            limit = f"container {name} limits.memory = {memory}" if memory else None
            values = readable(resources) or resources
            return clean(
                change_of(comp, f"container {name} limits.memory") or limit or f"container {name} resources: {values}"
            )
        return clean(change_of(comp, f"container {name} image") or f"container {name} image = {image}")
    workload = re.match(r"^workload (\w+): (.*)$", item, re.S)  # a pod spec field as the examination reads it
    if workload:
        head, *rest = workload.group(1).split("_")
        item = f"{head}{''.join(w.title() for w in rest)} = {workload.group(2)}"
    text = clean(item)
    if not text:
        return None
    key = _SETTING_KEY.match(text)
    if key and "->" not in text:
        text = clean(change_of(comp, key.group(1))) or text
    if "probe" in text:
        text = re.sub(
            r"\b(periodSeconds|timeoutSeconds|failureThreshold)=(\d+)(?! \(default)",
            lambda m: (
                m.group(0)
                + (f" (default {PROBE_DEFAULTS[m.group(1)]})" if int(m.group(2)) != PROBE_DEFAULTS[m.group(1)] else "")
            ),
            text,
        )
    return text


def change_of(comp: dict, setting: str) -> str | None:
    """The pod template change of one setting (`container c image`, `pod dnsPolicy`), when there is one."""
    return next((c for c in changed_values(comp) if re.match(re.escape(setting) + r"[.: ]", c)), None)


def key_fact(d: Diagnosis) -> str | None:
    if d.evidence_result is None:
        return None
    return next((e["text"] for e in d.evidence if e["id"] == d.evidence_result.choice and not e.get("context")), None)


def same_setting(d: Diagnosis, item: str | None) -> list[str]:
    """When the chosen fact names a setting (`container c args`) whose value cannot be stated as written (a cut-off
    JSON list, a script), the other evidence items about the same setting, then the setting by name: the fact stays
    on the setting the decision chose instead of moving to an unrelated change."""
    setting = _setting_of(item)
    if not setting:
        return []
    same = [e["text"] for e in d.evidence if e["text"] != item and _setting_of(e["text"]) == setting]
    return [*sorted(same, key=len), f"{setting} (selected evidence; value too long to state)"]


def _setting_of(item: str | None) -> str | None:
    found = _SETTING_KEY.match(_FRAMING.sub("", (item or "").strip()))
    return found.group(1) if found else None


def supporting_facts(d: Diagnosis) -> list[str]:
    """The other evidence items the key-evidence question ranked next to the chosen one, most supported first."""
    r = d.evidence_result
    if r is None:
        return []
    top = r.probabilities.get(r.choice, r.confidence)
    text = {e["id"]: e["text"] for e in d.evidence if not e.get("context")}
    return [text[i] for i, p in r.ranked() if i != r.choice and i in text and p >= COMPARABLE_SUPPORT * top]


def spec_checks(d: Diagnosis) -> list[str]:
    return [
        x
        for step in (d.investigation or {}).get("steps") or []
        if step.get("component") == d.component
        for x in step.get("spec_checks") or []
    ]


def changed_values(comp: dict) -> list[str]:
    """The workload's pod template differences (old -> new), restart annotations left out."""
    tc = comp.get("template_changes") or {}
    if tc.get("restart_only"):
        return []
    return [c for c in tc.get("changes") or [] if "pod-template-hash" not in c and "restartedAt" not in c]


def same_change(snapshot: ClusterSnapshot, cid: str) -> list[str]:
    """Other workloads whose pod template changed the same way (names aside) within the same few seconds: one
    change applied to several workloads, reported as one cause."""
    comp = snapshot.components.get(cid) or {}
    mine = _normalized(comp)
    if not mine:
        return []
    at = parse_time((comp.get("template_changes") or {}).get("changed_at"))
    out = []
    for other_id, other in snapshot.components.items():
        if other_id == cid or _normalized(other) != mine:
            continue
        other_at = parse_time((other.get("template_changes") or {}).get("changed_at"))
        gap = seconds_between(other_at, at) if other_at and at else None
        if gap is not None and abs(gap) <= SAME_CHANGE_SECONDS:
            out.append(other_id)
    return sorted(out)


def _normalized(comp: dict) -> tuple[str, ...]:
    """A workload's template changes with its own name (as a whole word: `rate` is not taken out of `hotel-rate`) and
    its container names taken out."""
    own = re.compile(rf"(?<![\w.-]){re.escape(comp.get('name') or '')}(?![\w.-])") if comp.get("name") else None
    return tuple(
        sorted(
            (own.sub("<name>", _CONTAINER.sub("container <c> ", c)) if own else _CONTAINER.sub("container <c> ", c))
            for c in changed_values(comp)
        )
    )


# --------------------------------------------------------------------------- the subject


@dataclass
class Subject:
    """What the Root cause line names: `phrase`, the component whose state shows the cause (`cid`), a fact the subject
    rule itself established, the objects whose writes make up the cause (for Changed), the fault type the subject's
    kind implies, and the other party found with it."""

    phrase: str
    cid: str | None = None
    fact: str | None = None
    objects: list[tuple[str, str, str | None]] = field(default_factory=list)
    kind_type: str | None = None
    contributing: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)  # components the cause reaches beyond tree.explained_by
    note: str | None = None  # what the platform does with the subject itself, ahead of the Mechanism


def subject_of(snapshot: ClusterSnapshot, d: Diagnosis) -> Subject:
    comp = snapshot.components.get(d.component) or {}
    if d.failed_reference:
        ref = d.failed_reference
        return Subject(
            phrase=f"Service `{ref['name']}` (missing) in namespace `{ref['namespace']}`",
            cid=d.component,
            fact=f"{ref['client']} cannot resolve {ref['name']}:{ref['port']}; the Service inventory confirms it is absent",
            kind_type="missing_object",
            related=[ref["client"]],
        )
    found = (comp and missing_subject(snapshot, d, comp)) or (d.fault_object and fault_object_subject(snapshot, d))
    if found:
        return found
    if not comp:
        return Subject(phrase="an object outside the application's workloads that the collected state does not name")
    own = [(comp["kind"], comp["name"], comp["namespace"])] + config_objects(comp)
    group = same_change(snapshot, d.component) if changed_values(comp) else []
    if group:
        names = ", ".join(f"`{snapshot.components[c]['name']}`" for c in [d.component, *group])
        phrase = f"{comp['kind']}s {names} in namespace `{comp['namespace']}` (the same change on {len(group) + 1})"
        return Subject(phrase=phrase, cid=d.component, objects=own)
    if d.blocked_input:
        return blocked_subject(d, comp, own)
    return Subject(phrase=component_phrase(comp), cid=d.component, objects=own)


# A consumer's setting that names the input it reads (messaging vocabulary); one naming where it writes is skipped.
_INPUT_SETTING = re.compile(r"^container \S+ env (\w*(?:TOPIC|QUEUE|STREAM|SUBJECT)\w*)=(\S+)$")
_OUTPUT_NAME = re.compile(r"OUT|REPLY|RESULT|DLQ|DEAD|RETRY|ERROR")


def blocked_subject(d: Diagnosis, comp: dict, own: list) -> Subject:
    """A consumer blocked on one input item: both ends are named, the consumer and the input its own settings say it
    reads, with the position it stalls at (the key=value tokens of the repeating failure)."""
    found = (_INPUT_SETTING.match(e["text"]) for e in d.evidence)
    setting = next((m for m in found if m and not _OUTPUT_NAME.search(m.group(1))), None)
    tokens = re.findall(r"\b[a-z_]+=\S+", (d.blocked_input or {}).get("evidence") or "")
    phrase = component_phrase(comp) + (f" and its input `{setting.group(2)}`" if setting else "")
    fact = (f"input {setting.group(1)}={setting.group(2)}" if setting else "its input") + " does not advance"
    return Subject(
        phrase=phrase, cid=d.component, fact=fact + (f" past {', '.join(tokens)}" if tokens else ""), objects=own
    )


def config_objects(comp: dict) -> list[tuple[str, str, str | None]]:
    out = []
    for key in comp.get("config_objects") or {}:
        kind, _, name = key.partition("/")
        out.append(("Secret" if kind == "secret" else "ConfigMap", name, comp["namespace"]))
    return out


def missing_subject(snapshot: ClusterSnapshot, d: Diagnosis, comp: dict) -> Subject | None:
    """A referenced object that does not exist is the subject, marked missing, when the decision points at it (its
    category is a missing object or a volume, or its key evidence names the object): a pod cannot start without a
    ConfigMap, Secret or PersistentVolumeClaim it requires, and a server no Service selects cannot be addressed."""
    category = d.category_result.choice if d.category_result else None
    key = key_fact(d) or ""
    refs = comp.get("config_refs") or {}
    present = set(comp.get("config_objects") or {})
    optional = set(refs.get("optional") or [])
    missing: list[tuple[str, str, str]] = []
    for kind, plural in (("ConfigMap", "configmaps"), ("Secret", "secrets")):
        for name in refs.get(plural) or []:
            if f"{kind.lower()}/{name}" not in present and f"{plural}/{name}" not in optional:
                volume = next(
                    (v["name"] for v in comp.get("volumes") or [] if v.get(kind[0].lower() + kind[1:]) == name), None
                )
                missing.append((kind, name, f"volume {volume} mounts it" if volume else "its containers read it"))
    for v in comp.get("volumes") or []:
        if v.get("pvc") and not v.get("phase"):  # a claim that exists has a phase
            missing.append(("PersistentVolumeClaim", v["pvc"], f"volume {v['name']} claims it"))
    named = [m for m in missing if f'"{m[1]}"' in key or f" {m[1]} " in f" {key} "]
    chosen = named or (missing if category in ("missing_object", "storage_volume") else [])
    where = describe(comp["kind"], comp["name"], None)
    if chosen:
        kind, name, how = chosen[0]
        return Subject(
            phrase=f"{kind} `{name}` (missing) in namespace `{comp['namespace']}`, referenced by {where}",
            cid=d.component,
            fact=f"{how}, and no {kind} {name} exists in namespace {comp['namespace']}",
            kind_type="missing_object",
        )
    ports = sorted({p for c in comp.get("containers") or [] if not c.get("init") for p in c.get("ports") or []})
    if category == "missing_object" and ports and not comp.get("services"):
        naming = sorted(c for c, o in snapshot.components.items() if d.component in (o.get("_deps") or {}))
        return Subject(
            phrase=f"the Service of {where} (missing) in namespace `{comp['namespace']}`",
            cid=d.component,
            fact=f"no Service selects its pods, which listen on {', '.join(map(str, ports))}",
            kind_type="missing_object",
            contributing=[f"{', '.join(naming[:3])} {'names' if len(naming) == 1 else 'name'} it as a dependency"]
            if naming
            else [],
            related=naming,
        )
    return None


def fault_object_subject(snapshot: ClusterSnapshot, d: Diagnosis) -> Subject:
    """A non-workload object carries the fault. A config object read by a CronJob makes the CronJob the subject (a
    scheduled job re-applies its change on every run, so it is what must be stopped); a config object of one workload
    is named with that workload; a Service with the workloads it selects; any other object with the workload it acts
    on."""
    fo = d.fault_object or {}
    kind, ns = fo["kind"], fo.get("namespace")
    names = [n for n in (fo.get("name") or "").split(", ") if n]
    listed = ", ".join(f"`{n}`" for n in names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    phrase = (
        f"{kind}s {listed}" + (f" in namespace `{ns}`" if ns else "")
        if len(names) > 1
        else describe(kind, fo.get("name") or "", ns)
    )
    objects = [(kind, n, ns) for n in names]
    users = users_of(snapshot, fo)
    comp = (snapshot.components.get(d.component) or {}) if d.component in users else {}
    record = change_record(snapshot, kind, fo.get("name"), ns)
    target = d.component if comp else next(iter(users), None)
    where = describe(snapshot.components[target]["kind"], snapshot.components[target]["name"], None) if target else ""
    subject = Subject(phrase=phrase, cid=target, fact=fault_object_fact(snapshot, fo), objects=objects)
    subject.kind_type = KIND_TYPES.get(kind)
    if fo.get("platform"):  # a platform layer failing for several workloads: no single object or workload is it
        from clients.jev_diag.v1_1.platform_events import PLATFORM_LAYERS

        n = len(users)
        subject.phrase = f"the cluster platform's {fo['name']}, failing for {n} workload{'s' if n != 1 else ''}"
        subject.fact = PLATFORM_LAYERS.get(fo.get("layer"), subject.fact)
        subject.kind_type = LAYER_TYPES.get(fo.get("layer"))
        return subject
    if comp:
        subject.objects.append((comp["kind"], comp["name"], comp["namespace"]))
    if kind in CONFIG_KINDS:
        reader = recurring_reader(snapshot, fo)
        if reader:
            writer = describe(reader["kind"], reader["name"], reader.get("namespace"))
            subject.phrase = f"{writer}, which reads {kind} `{fo['name']}`" + (f" and acts on {where}" if where else "")
            subject.objects = [(reader["kind"], reader["name"], reader.get("namespace")), *objects]
            subject.kind_type = None
            subject.note = "a CronJob starts a job on every scheduled run, so its write is applied again on each run"
        elif comp and len(users) <= 1 and ns in (snapshot.app.get("namespaces") or []):
            subject.phrase = f"{component_phrase(comp)}, through its {kind} `{fo['name']}`"
            subject.objects = [(comp["kind"], comp["name"], comp["namespace"]), *objects]
        elif len(users) > 1:
            subject.phrase += f", read by {', '.join(short_name(snapshot, u) for u in users[:3])}"
        elif where:
            subject.phrase += f", acting on {where}"
    elif kind == "Service":
        selected = users or ([target] if target else [])
        if selected:
            subject.phrase += f", selecting {', '.join(short_name(snapshot, u) for u in selected[:3])}"
    elif where:
        subject.phrase += f", acting on {where}"
    if target and record and target in users:
        t = snapshot.components[target]
        subject.objects.append((t["kind"], t["name"], t["namespace"]))
    return subject


def fault_object_fact(snapshot: ClusterSnapshot, fo: dict) -> str | None:
    """The faulty setting of a non-workload object, from its typed record where there is one."""
    kind, names = fo["kind"], (fo.get("name") or "").split(", ")
    if kind.endswith("WebhookConfiguration"):
        hooks = [w for w in snapshot.cluster.get("admission_webhooks") or [] if w.get("configuration") in names]
        for w in hooks:
            signal = " ".join(w.get("signals") or [])
            backend = w.get("backend") or {}
            if _UNTRUSTED.search(signal) and backend:
                return (
                    f"webhook {w.get('webhook')}: clientConfig.caBundle does not verify the certificate served by "
                    f"Service {backend.get('namespace')}/{backend.get('service')} (failurePolicy="
                    f"{w.get('failure_policy')})"
                )
    if kind == "NetworkPolicy":
        for p in snapshot.cluster.get("network_policies") or []:
            if p.get("name") in names and p.get("namespace") == fo.get("namespace"):
                selector = ", ".join(f"{k}={v}" for k, v in (p.get("pod_selector") or {}).items()) or "all pods"
                return (
                    f"podSelector {selector}, policyTypes {', '.join(p.get('policy_types') or [])}: {p.get('effect')}"
                )
    if kind in ("Role", "ClusterRole"):
        for role in snapshot.cluster.get("rbac") or []:
            if role.get("kind") == kind and role.get("name") in names:
                return f"rules: {'; '.join(role.get('rules') or []) or 'none'}"
    parts = (fo.get("signal") or "").split("; ")
    finding = clean("; ".join(p for p in parts if not p.startswith("now ")))
    return finding or clean(fo.get("signal"))


def users_of(snapshot: ClusterSnapshot, fo: dict) -> list[str]:
    """Resolve explicit relationships across all facts about an object; never infer them from timing."""
    records = [fo]
    for collection in ("findings", "rbac", "outside_objects", "namespace_constraints", "stuck_terminating"):
        records.extend(snapshot.cluster.get(collection) or [])
    users = {
        c
        for record in records
        if object_key(record) == object_key(fo)
        for c in record.get("components") or []
        if c in snapshot.components
    }
    kind, ns, name = fo["kind"], fo.get("namespace"), fo.get("name")
    for cid, comp in snapshot.components.items():
        if (kind, name, ns) in config_objects(comp):
            users.add(cid)
        if comp.get("namespace") == ns:
            if kind == "Service" and any(s.get("name") == name for s in comp.get("services") or []):
                users.add(cid)
            if kind == "PersistentVolumeClaim" and any(v.get("pvc") == name for v in comp.get("volumes") or []):
                users.add(cid)
    return sorted(users, key=lambda c: ("symptoms" not in (snapshot.components[c].get("evidence_kinds") or []), c))


def change_record(snapshot: ClusterSnapshot, kind: str, name: str | None, namespace: str | None) -> dict | None:
    for ch in snapshot.cluster.get("_changes") or []:
        if ch.get("kind") == kind and ch.get("name") == name and ch.get("namespace") in (namespace, None):
            return ch
    return None


def recurring_reader(snapshot: ClusterSnapshot, fo: dict) -> dict | None:
    """A CronJob written after the application was deployed whose spec names config object `fo` (its pods read it)."""
    for ch in snapshot.cluster.get("_changes") or []:
        if ch.get("kind") == "CronJob" and f'"{fo.get("name")}"' in (ch.get("content") or ""):
            return ch
    return None


def writer_of(snapshot: ClusterSnapshot, kind: str, name: str, namespace: str | None) -> str | None:
    """The field manager that wrote an object after the application was deployed (outside.py's record of it)."""
    for o in snapshot.cluster.get("outside_objects") or []:
        if o.get("kind") == kind and o.get("name") == name and o.get("namespace") in (namespace, None):
            for s in o.get("signals") or []:
                m = re.match(r"written by (.+?) after the application was deployed", s)
                if m:
                    return m.group(1)
    return None


# --------------------------------------------------------------------------- the fact and how it acts


def symptom_sources(snapshot: ClusterSnapshot, d: Diagnosis, subject: Subject) -> list[str]:
    """What the platform reported about the cause: events, container states, scheduler messages, admission and
    autoscaler failures (never application logs)."""
    comp = snapshot.components.get(subject.cid or "") or {}
    out = [f"{e.get('reason')} on {e.get('object')}: {e.get('message')}" for e in comp.get("warning_events") or []]
    out += [e["text"] for e in d.evidence if e["text"].startswith("event ")]
    for p in comp.get("pods") or []:
        out += [f"pod {p['name']}: {s}" for s in p.get("container_states") or []]
    out += [s for s in comp.get("signals") or [] if is_symptom(s)]
    out += (comp.get("hpa") or {}).get("problems") or []
    fo = d.fault_object or {}
    if fo:
        out.append(fo.get("signal") or "")
        names = (fo.get("name") or "").split(", ")
        out += [
            s
            for w in snapshot.cluster.get("admission_webhooks") or []
            if w.get("configuration") in names
            for s in w.get("signals") or []
        ]
    return [x for x in out if x and not _TREND.search(x)]


def platform_response(sources: list[str], kind: str | None) -> tuple[Response, str] | None:
    """The Kubernetes response behind the reported symptoms: one of the fact's own kind first, else the first in table
    order, with the report that shows it."""
    found = [(r, s) for r in RESPONSES for s in sources if r.symptom.search(s)]
    return next((x for x in found if x[0].kind == kind), found[0] if found else None)


def governing_setting(snapshot: ClusterSnapshot, d: Diagnosis, subject: Subject, response: Response) -> str | None:
    """The value of the setting that `response` consults, from the typed spec (its pod template change when it
    changed)."""
    comp = snapshot.components.get(subject.cid or "") or {}
    main = [c for c in comp.get("containers") or [] if not c.get("init")]
    evidence = [e["text"] for e in d.evidence]
    if response.setting == "memory_limit":
        for c in main:
            limit = (c.get("limits") or {}).get("memory")
            if limit:
                return (
                    change_of(comp, f"container {c['name']} limits.memory")
                    or f"container {c['name']} limits.memory = {limit}"
                )
    if response.setting == "requests":
        for c in main:
            if c.get("requests"):
                return (
                    change_of(comp, f"container {c['name']} requests")
                    or f"container {c['name']} requests: {', '.join(flat(c['requests']))}"
                )
    if response.setting == "image":
        changed = [x for c in main if (x := change_of(comp, f"container {c['name']} image"))]
        return next(iter(changed), None) or next(
            (f"container {c['name']} image = {c['image']}" for c in main if c.get("image")), None
        )
    if response.setting == "host_port":
        ports = [
            f"container {c['name']} hostPort = {', '.join(map(str, c['host_ports']))}"
            for c in comp.get("containers") or []
            if c.get("host_ports")
        ]
        return "; ".join(ports) or None
    if response.setting == "anti_affinity" and comp.get("anti_affinity_required"):
        replicas = (comp.get("replicas") or {}).get("desired")
        return f"pod anti-affinity requiredDuringSchedulingIgnoredDuringExecution with replicas = {replicas}"
    if response.setting == "node_selector":
        return next((e for e in evidence if e.startswith("workload node_selector:")), None) or change_of(
            comp, "pod nodeSelector"
        )
    if response.setting == "taint":
        tainted = Counter(
            t
            for n in snapshot.cluster.get("nodes") or []
            for t in n.get("taints") or []
            if "node-role.kubernetes.io" not in t
        )
        if tainted:
            taint, n = tainted.most_common(1)[0]
            return f"{n} node(s) carry taint {taint}, which the pods do not tolerate"
    if response.setting in ("liveness", "readiness"):
        return next((e for e in evidence if re.match(rf"container \S+ {response.setting} probe", e)), None)
    return None


def fact_of(snapshot: ClusterSnapshot, d: Diagnosis, subject: Subject) -> str | None:
    """The faulty setting as field = value: what the subject rule found, else the key evidence when it is a setting,
    else the setting behind the symptom the key evidence (or the component) shows, else the settings ranked next to
    the key evidence, the pod template changes and the failed spec checks."""
    comp = snapshot.components.get(subject.cid or "") or {}
    key = key_fact(d)
    candidates = [subject.fact, key, *same_setting(d, key)]
    sources = ([key] if key and is_symptom(key) else []) + symptom_sources(snapshot, d, subject)
    response = platform_response(sources, None)
    if response:
        candidates.append(governing_setting(snapshot, d, subject, response[0]))
    candidates += supporting_facts(d) + changed_values(comp) + spec_checks(d) + diagnostic_signals(comp)
    provenance = {e["text"]: e for e in [*(comp.get("change_evidence") or []), *d.evidence]}
    owners = {(kind.lower(), ns, name) for kind, name, ns in subject.objects}
    for item in candidates:
        entry = provenance.get(item) or {}
        source = entry.get("source_object")
        if entry.get("context") or (source and object_key(source) not in owners):
            continue
        value = value_text(item, d, comp)
        if value:
            grace = next(
                (e["text"] for e in d.evidence if e["text"].startswith("pod terminationGracePeriodSeconds")), None
            )
            if grace and "liveness probe" in value:  # a liveness restart kills the container within the grace period
                value += f"; {grace}"
            return value
    return None


def fault_kind(d: Diagnosis, subject: Subject, fact: str | None) -> str | None:
    """The fault type: the subject's kind, else the faulty field's class; the generic config-value class gives way to
    the investigation's category when that is a sure, more specific one."""
    category = d.category_result.choice if d.category_result else None
    sure = d.category_result is not None and d.category_result.confidence >= 0.5 and category != OTHER_OPTION
    found = subject.kind_type or next((k for k, p in FIELD_TYPES if fact and re.search(p, fact)), None)
    if found == "config_value" and sure:
        return category
    return found or (category if category != OTHER_OPTION else None)


def quote(line: str) -> str:
    """One log line as quotable text: the message of a structured line, without timestamps or the container prefix."""
    prefix = _NAME_PREFIX.match(line)  # "container-name: " as the collector writes it, never "FATAL: " etc.
    body = line[prefix.end() :] if prefix else line
    msg = _JSON_MSG.search(body)
    if msg:
        err = _JSON_ERR.search(body)
        body = msg.group("msg") + (f": {err.group('err')}" if err else "")
    body = _TIMESTAMP.sub("", body.strip())
    return body[:QUOTE_CHARS].rstrip(" .,;") + ("..." if len(body) > QUOTE_CHARS else "")


def revision_quote(comp: dict) -> str | None:
    """The most severe still-occurring error line of the component, only when all its pods run one pod template (log
    lines are not attributed to pods, and an older revision's errors cannot show what the newer one does). Stack
    frames and telemetry export errors are never quoted."""
    if len({p.get("template_hash") for p in comp.get("pods") or []}) > 1:
        return None
    crashed = [  # a crashed container's last lines come from the same pod, run before its restart
        {"line": line, "count": 0}
        for lines in (comp.get("previous_container_logs") or {}).values()
        for line in lines or []
    ]
    lines = [
        s
        for s in [*(comp.get("log_signals") or []), *crashed]
        if s.get("state") != "stopped"
        and s.get("line")
        and is_error_line(s["line"])
        and not TELEMETRY_RE.search(s["line"])
        and not _STACK_FRAME.search(quote(s["line"]))
    ]

    def severity(s: dict) -> tuple:
        text = s["line"]
        return (-next((i for i, p in enumerate(_SEVERITY) if p.search(text)), len(_SEVERITY)), s["count"] or 0)

    best = max(lines, key=severity, default=None)
    return quote(best["line"]) if best else None


def selector_text(selector: dict) -> str:
    return ", ".join(x.removeprefix("matchLabels.") for x in flat(selector))


def cut_middle(text: str, limit: int) -> str:
    """At most `limit` characters keeping both ends: a wrapped error's specific cause is at its end."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head].rstrip() + " … " + text[-(limit - head - 3) :].lstrip()


def blocked_mechanism(b: dict) -> str:
    if b.get("source") == "broker":
        return f"consumer group {b.get('group')} is stalled at offset {b.get('offset')} of topic {b.get('topic')}"
    evidence = f': "{quote(b["evidence"])}"' if b.get("evidence") else ""
    return f"one input item fails processing again and again and blocks the items behind it{evidence}"


def mechanism(
    snapshot: ClusterSnapshot, d: Diagnosis, subject: Subject, fact: str | None, kind: str | None
) -> str | None:
    """What Kubernetes does with the faulty setting, then the symptom: the platform's response of the fault's own kind
    with the event that shows it; else the setting's own semantics (SETTING_RESPONSES); else a platform response to
    another setting, named first. The component's own finding that says why, or one log quote, shows the symptom when
    no event does."""
    comp = snapshot.components.get(subject.cid or "") or {}
    key = key_fact(d)
    sources = ([key] if key and is_symptom(key) else []) + symptom_sources(snapshot, d, subject)
    found = platform_response(sources, kind)
    parts = [subject.note] if subject.note else []
    parts += [blocked_mechanism(d.blocked_input)] if d.blocked_input and comp else []
    semantics = next(
        (
            does + (f" (here: {m.group(0)})" if (m := re.search(detail[0], fact) if detail else None) else "")
            for pattern, does, *detail in SETTING_RESPONSES
            if fact and re.search(pattern, fact)
        ),
        None,
    )
    if found and (found[0].kind == kind or not semantics):
        response, seen = found
        shown = clean(re.sub(r"^event (\S+) x\d+ on ", r"\1 on ", seen)) or seen
        setting = value_text(governing_setting(snapshot, d, subject, response), d, comp)
        known = setting and fact and _squeeze(setting)[:40] in _squeeze(fact)
        lead = f"{cut_change(setting, 160)}; " if setting and not known else ""
        parts.append(f"{lead}{response.does}; {cut_middle(shown, 240)}")
        return "; ".join(parts)
    if semantics:
        parts.append(semantics)
    own = [s for s in diagnostic_signals(comp) if s != fact and not s.startswith(("errors logged by", "server "))]
    own = [s for s in own if not _TREND.search(s) and not re.search(r"ready endpoints|desired replicas", s)]
    login = next((s for s in comp.get("signals") or [] if s.startswith("server ") and "rejects logins" in s), None)
    causal = next((s for s in own if _BECAUSE.search(s)), None) or next((s for s in own if _CAUSAL.search(s)), None)
    line = revision_quote(comp)
    shown_text = " ".join(parts) + (fact or "")
    quoted = f'the application logs "{line}"' if line and line not in shown_text else None
    shown = [login] + ([quoted] if semantics and quoted else [causal, quoted])
    for item in shown:
        text = (clean(item) if item is not quoted else item) if item else None
        if text and (not fact or text not in fact) and not any(text in p or p in text for p in parts):
            parts.append(cut(text, 260))
    return "; ".join(parts) or None


def _squeeze(text: str) -> str:
    return re.sub(r"[\s=:]+", "", text)


# --------------------------------------------------------------------------- the other lines


def changed(snapshot: ClusterSnapshot, subject: Subject) -> str | None:
    """When each object of the cause was written, by whom, how long before the first symptom, and its values: every
    pod template change as old -> new, a modified object's new values, a created object's content when it is a few
    values. Objects of one kind written together with nothing else to say are named in one item."""
    onset = parse_time(snapshot.cluster.get("symptom_onset"))
    items: list[dict] = []
    for kind, name, ns in dict.fromkeys(subject.objects):
        comp = next(
            (c for c in snapshot.components.values() if (c["kind"], c["name"], c["namespace"]) == (kind, name, ns)),
            None,
        )
        record = change_record(snapshot, kind, name, ns)
        values = [cut_change(v, VALUE_CHARS) for v in map(clean, changed_values(comp) if comp else []) if v]
        if (comp and not values and not (record and record.get("values"))) or not (comp or record):
            continue
        record = record or {}
        at = record.get("at") or ((comp or {}).get("template_changes") or {}).get("changed_at")
        created = record.get("created_after_targets_s") is not None and record.get("modified_after_creation_s") is None
        if not values:
            values = [cut_change(v, VALUE_CHARS) for v in map(clean, record.get("values") or []) if v]
            content = readable(record["content"]) if created and record.get("content") else None
            if content and len(content) <= VALUE_CHARS and not content.endswith("…") and not _ENCODED.search(content):
                values.append(content)
            values = [v for v in values if v]
        verb = "created" if created else "modified"
        writer = writer_of(snapshot, kind, name, ns)
        last = items[-1] if items else None
        if (
            last
            and not values
            and not last["values"]
            and (last["kind"], last["verb"], last["writer"])
            == (
                kind,
                verb,
                writer,
            )
        ):
            last["names"].append(name)
            continue
        items.append({"kind": kind, "names": [name], "verb": verb, "at": at, "writer": writer, "values": values})
    out = []
    for item in items:
        gap = seconds_between(onset, parse_time(item["at"])) if onset and item["at"] else None
        when = "" if gap is None else f", {abs(gap)}s {'before' if gap >= 0 else 'after'} the first symptom"
        names = item["names"]
        listed = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        head = f"{item['kind']}{'s' if len(names) > 1 else ''} {listed} {item['verb']}"
        head += (f" at {item['at']}" if item["at"] else "") + (f" by {item['writer']}" if item["writer"] else "")
        out.append(head + when + (f": {'; '.join(item['values'])}" if item["values"] else ""))
    return cut("; ".join(out), MAX_CHANGED) if out else None


def state_of(comp: dict) -> str:
    """A component's symptoms from its typed state: replicas, pod phases and container states, restarts, a container
    near its CPU limit (usage alone does not establish throttling), Services without endpoints, failing
    calls by class (telemetry export left out), jobs that do not complete, an autoscaler that cannot scale, else its
    still-occurring error log lines. Empty when it shows none."""
    out = []
    r = comp.get("replicas") if comp.get("kind") in ("Deployment", "StatefulSet", "ReplicaSet", "DaemonSet") else None
    r = r or {}
    if r.get("desired") == 0:
        out.append("scaled to 0 replicas")
    elif r.get("desired") is not None and (r.get("ready") or 0) < r["desired"]:
        out.append(f"{r.get('ready') or 0}/{r['desired']} replicas ready")
    reasons: Counter = Counter()
    for p in comp.get("pods") or []:
        if p.get("phase") == "Pending":
            reasons["Pending"] += 1
        for s in p.get("container_states") or []:
            m = _STATE.match(s)
            if m and m.group(2) not in ("Completed",):
                reasons[m.group(2)] += 1
    out += list(reasons)
    restarts = sum(p.get("restarts") or 0 for p in comp.get("pods") or [])
    if restarts:
        out.append(f"{restarts} restarts")
    out += sorted(
        {
            f"container {u['container']} near its CPU limit {u['cpu_limit']}"
            for u in comp.get("resource_usage") or []
            if (parse_cpu_millis(u.get("cpu_limit")) or 0) > 0
            and (parse_cpu_millis(u.get("cpu")) or 0) >= CPU_SATURATED * parse_cpu_millis(u.get("cpu_limit"))
        }
    )
    out += [
        f"Service {s['name']} has no ready endpoints"
        for s in comp.get("services") or []
        if s.get("ready_endpoints") == 0
    ]
    classes: Counter = Counter()
    for f in comp.get("log_findings") or []:
        for klass in f.get("classes") or []:
            if not TELEMETRY_RE.search(klass):
                classes[klass] += int(f.get("count") or 1)
    out += [f"{k} errors ({n} lines)" for k, n in classes.most_common(2)]
    active = (comp.get("cronjob") or {}).get("active_jobs") or 0
    if active > 1:
        out.append(f"{active} jobs active without completing")
    out += [cut(p, 80) for p in (comp.get("hpa") or {}).get("problems") or []][:1]
    if not out:
        errors = sum(
            s.get("count") or 1
            for s in comp.get("log_signals") or []
            if s.get("state") != "stopped" and is_error_line(s.get("line") or "") and not TELEMETRY_RE.search(s["line"])
        )
        out += [f"{errors} error log lines still occurring"] if errors else []
    return ", ".join(out)


def impact(snapshot: ClusterSnapshot, d: Diagnosis, subject: Subject) -> list[str]:
    """The subject's state, then the failing components the cause accounts for, those with symptoms of their own
    first."""
    from clients.jev_diag.v1_1.tree import explained_by  # tree imports this module

    out = []
    own = snapshot.components.get(subject.cid or "")
    if own and state_of(own):
        out.append(f"{short_name(snapshot, subject.cid)}: {state_of(own)}")
    callers = (own or {}).get("referenced_by_errors_from") or {}  # components whose errors name the subject
    explained = explained_by(snapshot, subject.cid or d.component, d.fault_object) | set(subject.related) | set(callers)
    states = {
        c: state_of(snapshot.components[c])
        or ", ".join(f"{k} errors naming it ({n} lines)" for k, n in (callers.get(c) or {}).items())
        for c in explained
        if c != subject.cid and c in snapshot.components
    }
    others = sorted((c for c, s in states.items() if s), key=lambda c: ("errors" in states[c], c))
    out += [f"{short_name(snapshot, c)}: {states[c]}" for c in others[:MAX_IMPACT]]
    if len(others) > MAX_IMPACT:
        out.append(f"{len(others) - MAX_IMPACT} more failing components")
    return out


def scope(snapshot: ClusterSnapshot, d: Diagnosis) -> str | None:
    """How far a policy cause reaches beyond the workload it broke: the requests an admission webhook intercepts, the
    pods a NetworkPolicy selects, or the other workloads a namespace policy applies to (for a LimitRange, those whose
    settings it already rejects)."""
    fo = d.fault_object or {}
    kind, ns = fo.get("kind") or "", fo.get("namespace")
    if kind.endswith("WebhookConfiguration"):
        hooks = [
            w
            for w in snapshot.cluster.get("admission_webhooks") or []
            if w.get("configuration") in (fo.get("name") or "").split(", ")
        ]
        if not hooks:
            return None
        rules = dict.fromkeys(r for w in hooks for r in w.get("rules") or [])
        wheres = dict.fromkeys(
            f"namespaces matching {selector_text(w['namespace_selector'])}"
            if w.get("namespace_selector")
            else "every namespace"
            for w in hooks
        )
        narrowed = dict.fromkeys(selector_text(w["object_selector"]) for w in hooks if w.get("object_selector"))
        text = f"it intercepts {', '.join(rules) or 'its rules'} in {'; '.join(wheres)}"
        some = f"{len([w for w in hooks if w.get('object_selector')])} of {len(hooks)}" if len(hooks) > 1 else "it"
        return text + (f"; {some} only for objects labeled {'; '.join(narrowed)}" if narrowed else "")
    if kind == "NetworkPolicy":
        users = users_of(snapshot, fo)
        return f"it applies to pods of {', '.join(short_name(snapshot, u) for u in users)}" if users else None
    if kind not in ("LimitRange", "ResourceQuota"):
        return None
    others = [c for cid, c in snapshot.components.items() if c["namespace"] == ns and cid != d.component]
    if kind == "LimitRange":
        limits = next(
            (
                c.get("limits") or []
                for c in snapshot.cluster.get("namespace_constraints") or []
                if c.get("kind") == kind and c.get("name") == fo.get("name") and c.get("namespace") == ns
            ),
            [],
        )
        others = [c for c in others if checks.violates_limit_range(c, limits)]
        if others:
            names = ", ".join(f"`{c['name']}`" for c in others[:3])
            more = f" and {len(others) - 3} more" if len(others) > 3 else ""
            return (
                f"it applies to every pod created in namespace `{ns}`; {names}{more} also "
                f"{'violates' if len(others) == 1 else 'violate'} it, so their pods are rejected when next created"
            )
    return f"it applies to every pod created in namespace `{ns}`"


def contributing(snapshot: ClusterSnapshot, d: Diagnosis, subject: Subject) -> list[str]:
    """The other party of the cause, from typed records."""
    out = list(subject.contributing)
    fo = d.fault_object or {}
    names = (fo.get("name") or "").split(", ")
    comp = snapshot.components.get(subject.cid or "") or {}
    backends: dict[str, list[str]] = {}
    for w in snapshot.cluster.get("admission_webhooks") or []:
        backend = w.get("backend") or {}
        if w.get("configuration") in names and backend and fo.get("kind", "").endswith("WebhookConfiguration"):
            backends.setdefault(backend.get("namespace"), {})[backend.get("service")] = w.get("backend_ready_endpoints")
    for ns, services in backends.items():
        ready = set(services.values())
        n = next(iter(ready)) if len(ready) == 1 else None
        each = f" ({n} ready endpoints{' each' if len(services) > 1 else ''})" if n is not None else ""
        out.append(f"webhook backend Services {', '.join(services)} in namespace {ns}{each}")
    for ns in sorted(n for n in backends if n):  # policies written after deploy that limit traffic to the backends
        limiting = [
            ch["name"]
            for ch in snapshot.cluster.get("_changes") or []
            if ch.get("kind") == "NetworkPolicy"
            and ch.get("namespace") == ns
            and '"Ingress"' in (ch.get("content") or "")
        ]
        if limiting:
            out.append(
                f"NetworkPolicies {', '.join(limiting)} limit ingress to pods in namespace {ns}, where the backends run"
            )
    for role in snapshot.cluster.get("rbac") or []:
        if role.get("kind") == fo.get("kind") and role.get("name") in names:
            out.append(
                f"bound via {', '.join(role.get('bindings') or []) or 'no binding'} to service account(s) "
                f"{', '.join(role.get('service_accounts') or []) or 'none'}"
            )
    for st in snapshot.cluster.get("stuck_terminating") or []:
        if subject.cid in (st.get("components") or []):
            out.append(
                f"{st.get('kind')} {st.get('name')} has been terminating for {st.get('terminating_for_s')}s, held by "
                f"finalizer {', '.join(st.get('finalizers') or [])}: {st.get('permission_gap')}"
            )
    hpa = comp.get("hpa") or {}
    if hpa.get("problems"):
        out.append(f"HorizontalPodAutoscaler {hpa.get('name')} (min {hpa.get('min')}, max {hpa.get('max')})")
    for p in snapshot.cluster.get("call_paths") or []:
        if subject.cid in (p.get("caller"), p.get("callee")):
            out.append(
                f"{p['caller']} calls {p['callee']} with {', '.join(p.get('caller_settings') or []) or 'no settings found'}"
                f"; {p['callee']} capacity: {', '.join(p.get('callee_settings') or []) or 'no settings found'}"
            )
    if comp.get("login_candidates"):
        accounts = ", ".join(comp.get("rejected_accounts") or {})
        out.append(
            f"clients that may present the rejected credentials{f' for {accounts}' if accounts else ''}: {', '.join(comp['login_candidates'][:3])}"
        )
    if any("Preempted by" in s for s in symptom_sources(snapshot, d, subject)):
        for o in snapshot.cluster.get("outside_objects") or []:
            if o.get("kind") == "PriorityClass" and o.get("settings"):
                out.append(f"PriorityClass {o['name']} ({o['settings']})")
    return list(dict.fromkeys(out))


def evidence(d: Diagnosis, comp: dict, shown: str, fact: str | None) -> list[str]:
    """Labeled value-bearing facts not stated above: the key evidence and the items ranked next to it, the failed
    spec checks, the full warning events, then the component's other findings."""
    candidates = [x for x in (key_fact(d), *supporting_facts(d)) if not fact or value_text(x, d, comp) != fact]
    candidates += [f"check: {x}" for x in spec_checks(d)]
    events = sorted(comp.get("warning_events") or [], key=lambda e: -(e.get("count") or 0))
    once = {(e.get("reason"), e.get("object")): e for e in reversed(events)}  # one per reason and object
    candidates += [
        f"event {e.get('reason')} x{e.get('count')} on {e.get('object')}: {e.get('message')}"
        for e in events
        if e.get("reason") != "BackOff" and once[(e.get("reason"), e.get("object"))] is e
    ]
    candidates += [s for s in comp.get("signals") or [] if not is_symptom(s) and not _WRITE.match(s)]
    out = []
    for item in candidates:
        labeled = label(item) if item and not _TREND.search(item) else None
        body = _squeeze(labeled.split(": ", 1)[1]).rstrip("….") if labeled else ""
        ends = (body[:50], body[-50:])
        if labeled and not any(e in _squeeze(x) for e in ends for x in [shown, *out]):
            out.append(labeled)
    return out[:MAX_EVIDENCE]


def label(item: str | None) -> str | None:
    """An evidence item as `kind: value`, with the item's framing (counts, recency, revision) taken out."""
    if not item:
        return None
    m = re.match(r"^(?:[a-z]+/\S+: )?(?:log x\d+ \([^)]*\)|previous log \S+): (.*)$", item)
    if m:
        return f'log: "{quote(m.group(1))}"'
    m = re.match(r"^repeated failure x(\d+) over (\d+)s with unchanged tokens .*?: (.*)$", item)
    if m:
        return f'log ({m.group(1)} times over {m.group(2)}s): "{quote(m.group(3))}"'
    m = re.match(r"^event (\S+) x\d+ on (\S+): (.*)$", item, re.S)
    if m:
        return f"event {m.group(1)} on {m.group(2)}: {cut_middle(m.group(3), 240)}"
    if item.startswith("check: "):
        text = clean(item[7:])
        return f"check: {cut(text, 240)}" if text else None
    if _CONFIG_KEY.match(item) and len(item) > VALUE_CHARS:
        return None
    text = "; ".join(p for p in (clean(item) or "").split("; ") if not p.startswith(("it holds ", "it runs ")))
    if len(text) < 8:
        return None
    setting = re.match(r"^(?:container \S+ |pod [a-zA-Z]+[ :]|(?:ConfigMap|Secret) \S+ key |template |volume )", text)
    return f"{'setting' if setting else 'observed'}: {cut(text, 240)}"


# --------------------------------------------------------------------------- assembly


def build_report(snapshot: ClusterSnapshot, d: Diagnosis) -> str:
    text = cause_block(snapshot, d)
    if d.second is not None:
        text += "\n\nA second, independent root cause:\n" + cause_block(snapshot, d.second)
    return text


def cause_block(snapshot: ClusterSnapshot, d: Diagnosis) -> str:
    if d.unresolved:
        candidates = [step["component"] for step in (d.investigation or {}).get("steps", []) if step.get("component")]
        return (
            "Root cause: unresolved. The two comparison orders disagree, and no candidate has independent "
            "origin support." + (f"\nExamined: {', '.join(dict.fromkeys(candidates))}." if candidates else "")
        )
    subject = subject_of(snapshot, d)
    comp = snapshot.components.get(subject.cid or "") or snapshot.components.get(d.component) or {}
    fact = fact_of(snapshot, d, subject)
    head = f"Root cause: {subject.phrase}"
    root = head + (f": {shorten(fact.rstrip('.'), max(60, MAX_ROOT - len(head) - 3))}." if fact else ".")
    root = cut(root, MAX_ROOT)
    kind = fault_kind(d, subject, fact)
    lines = [root]
    how = mechanism(snapshot, d, subject, fact, kind)
    if how:
        lines.append(f"Mechanism: {cut(how, 400)}.")
    specific = f" ({cut_change(fact.rstrip('.'), 120)})" if fact else ""
    lines.append(f"Fault type: {kind.replace('_', ' ') if kind else 'not classified'}{specific}.")
    when = changed(snapshot, subject)
    if when:
        lines.append(f"Changed: {when}.")
    reach = scope(snapshot, d)
    tail_lines = []
    if reach:
        tail_lines.append(f"Scope: {cut(reach, 300)}.")
    other = contributing(snapshot, d, subject)
    if other:
        tail_lines.append(f"Contributing: {cut('; '.join(other), 300)}.")
    hit = impact(snapshot, d, subject)
    shown = "\n".join(lines + tail_lines + hit)
    found = evidence(d, comp, shown, fact)
    while True:
        body = lines + ([f"Impact: {'; '.join(hit)}."] if hit else []) + tail_lines
        body += [f"Evidence: {'; '.join(found)}."] if found else []
        text = "\n".join(body)
        if len(text) <= MAX_TOTAL:
            return text
        if found:
            found.pop()
        elif len(hit) > 1:
            hit.pop()
        else:  # the longest line after the Root cause gives up what is over
            longest = max(range(1, len(lines)), key=lambda i: len(lines[i]), default=None)
            if longest is None or len(lines[longest]) <= 120:
                return cut(text, MAX_TOTAL)
            lines[longest] = cut(lines[longest], max(120, len(lines[longest]) - (len(text) - MAX_TOTAL)))
