"""Read-only tools written by an LLM for the environment at hand ("crafted tools").

The fixed tool menu covers Kubernetes objects, logs, Prometheus conventions and a table of backend health
commands. An unfamiliar environment brings things it does not cover: other backends, CRD-managed objects,
application metrics with their own names. A crafted tool lets the LLM fill such a gap without writing code: it is
a JSON spec that combines at most MAX_STEPS steps from a fixed set of read-only PRIMITIVES, which this module
validates, runs and summarises. The decision model then chooses crafted tools like any other.

Spec:
  {"name": "snake_case_name",
   "reveals": "one sentence: what the investigator learns",
   "applies_to": {"role": "<backend role>" | null, "image_contains": "<substring>" | null},
   "steps": [<step>, ...]}                      # 1..MAX_STEPS
Steps (placeholders {namespace} {name} {pod} {pod_regex} {service} are filled per target component):
  {"op": "k8s_get",   "resource": "<api resource>", "name": "<name or template, optional>",
                      "fields": ["status.phase", ...]}            # list or get; never Secrets
  {"op": "promql",    "query": "<instant PromQL>"}                # evaluated over the last 15m, summarised
  {"op": "logs_grep", "pattern": "<regex>", "previous": false}    # count + last matching lines of the pod
  {"op": "exec",      "command": ["<binary>", "<arg>", ...]}      # whitelisted read-only binaries only
  {"op": "http_get",  "port": <int>, "path": "/...", "grep": "<regex, optional>"}  # API server proxy, else curl/wget
                                                                                 # from a pod of the namespace

Nothing here recognises a fault: crafted tools observe state, and validation rejects anything that could change
it (write verbs, shells, Secret reads, credential printing).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from clients.jev_agent import metrics
from clients.jev_agent.roles import backend_role
from clients.jev_agent.types import COMPONENT, MAX_FINDINGS, AgentContext, ToolResult, ToolSpec
from clients.jev_diag import collector, probes

logger = logging.getLogger("all.jev_agent.crafted")

PREFIX = "x_"  # registry names of crafted tools
MAX_STEPS = 3
MAX_ITEMS = 5
PRIMITIVES = ("k8s_get", "promql", "logs_grep", "exec", "http_get")
FORBIDDEN_RESOURCES = {"secrets", "secret", "serviceaccounts/token", "tokenreviews", "subjectaccessreviews"}

# Binaries that only read when called without write subcommands, with the memory they need in the container.
READONLY_BINARIES: dict[str, int] = {
    "valkey-cli": probes.NATIVE_TOOL_BYTES,
    "redis-cli": probes.NATIVE_TOOL_BYTES,
    "pg_isready": probes.NATIVE_TOOL_BYTES,
    "mysqladmin": probes.NATIVE_TOOL_BYTES,
    "nats": 32 * 1024**2,
    "rabbitmqctl": 128 * 1024**2,
    "rabbitmq-diagnostics": 128 * 1024**2,
    "etcdctl": 32 * 1024**2,
    "kafka-topics.sh": 256 * 1024**2,
    "kafka-consumer-groups.sh": 256 * 1024**2,
    "kafka-log-dirs.sh": 256 * 1024**2,
    "nslookup": probes.NATIVE_TOOL_BYTES,
    "getent": probes.NATIVE_TOOL_BYTES,
    "dig": probes.NATIVE_TOOL_BYTES,
    "curl": 8 * 1024**2,
    "wget": probes.NATIVE_TOOL_BYTES,
    "cat": probes.NATIVE_TOOL_BYTES,
    "ls": probes.NATIVE_TOOL_BYTES,
    "df": probes.NATIVE_TOOL_BYTES,
    "ps": probes.NATIVE_TOOL_BYTES,
}
# Words (any case) that make a command able to change state or reveal credentials.
FORBIDDEN_WORDS = {
    "set",
    "del",
    "delete",
    "unlink",
    "flushall",
    "flushdb",
    "drop",
    "truncate",
    "insert",
    "update",
    "create",
    "alter",
    "grant",
    "revoke",
    "shutdown",
    "kill",
    "restart",
    "stop",
    "start",
    "purge",
    "reset",
    "write",
    "put",
    "post",
    "patch",
    "acl",
    "debug",
    "migrate",
    "move",
    "rename",
    "expire",
    "eval",
    "script",
    "publish",
    "env",
    "printenv",
    "auth",
}
# Flags (exact case: `ps -o` formats output, `curl -o` writes a file) that change state or carry credentials.
FORBIDDEN_FLAGS = {
    "--delete",
    "--reset-offsets",
    "--execute",
    "--alter",
    "--create",
    "-a",
    "--pass",
    "--password",
    "--user",
    "-u",
}
# HTTP clients get an allow-list instead: GET requests that print to stdout. Flags with a value take the next
# argument (or `--flag=value`).
HTTP_CLIENT_FLAGS = {
    "curl": {
        "plain": {
            "-s",
            "-S",
            "-f",
            "-i",
            "-I",
            "-k",
            "-L",
            "-v",
            "--silent",
            "--show-error",
            "--fail",
            "--include",
            "--head",
            "--insecure",
            "--location",
            "--compressed",
            "--http0.9",
            "--http1.0",
            "--http1.1",
            "--http2",
            "--verbose",
            "--no-progress-meter",
        },
        "short_combinable": set("sSfiIkLv"),
        "valued": {"-m", "--max-time", "--connect-timeout", "-H", "--header", "-w", "--write-out", "--retry"},
    },
    "wget": {
        "plain": {"-q", "-S", "--quiet", "--server-response", "--spider", "--no-check-certificate", "-qO-", "-O-"},
        "short_combinable": set("qS"),
        "valued": {"-O", "-T", "--timeout", "-t", "--tries", "--header", "--output-document"},
    },
}
# Request headers an HTTP client may send (nothing that authenticates).
SAFE_HEADERS = {"accept", "accept-encoding", "host", "user-agent", "content-type"}
URL_RE = re.compile(r"^https?://[A-Za-z0-9._~\-]+(:\d+)?(/[A-Za-z0-9._~\-/%?=&:,+*@!{}]*)?$")
SENSITIVE_PATH = re.compile(r"(?i)secret|token|passw|credential|\.key\b|\.pem\b|/proc/\d+/environ|/proc/self/environ")
SHELL_CHARS = re.compile(r"[;&|`$<>]")
KAFKA_ENV = ["env", "KAFKA_HEAP_OPTS=-Xms32m -Xmx64m", "KAFKA_OPTS=", "KAFKA_JMX_OPTS="]
URL_PATH_RE = re.compile(r"^/[A-Za-z0-9._~\-/%?=&:,+*@!{}]*$")
MAX_QUERY_CHARS = 1000
EXEC_TRAILER = re.compile(r"^command terminated with exit code \d+$")
HTTP_TIMEOUT_S = "5"
MAX_HTTP_ATTEMPTS = 4
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


@dataclass
class Crafted:
    """A read-only tool written by the LLM for this environment: named steps over a few safe primitives."""

    name: str
    reveals: str
    steps: list[dict]
    applies_to: dict = field(default_factory=dict)
    source: str = "explore"  # "explore" (before the investigation) | "in_task"
    only_target: str | None = None  # an in-task tool is written for one lead: it applies to that component only

    @property
    def tool_name(self) -> str:
        """The registry name of the tool (crafted tools share a prefix so they never shadow built-in ones)."""
        return PREFIX + self.name

    def to_dict(self) -> dict:
        """The tool as written by the LLM (for the trace and the explore cache)."""
        return {"name": self.name, "reveals": self.reveals, "applies_to": self.applies_to, "steps": self.steps}


# --------------------------------------------------------------------------- validation


def _words(parts: list[str]) -> list[str]:
    """The lowercased words of command parts, split at whitespace, '=' and ','."""
    return [w for p in parts for w in re.split(r"[\s=,]+", str(p).lower()) if w]


def _url_words(url: str) -> list[str]:
    """The lowercased words of a URL's path and query."""
    path = re.sub(r"^https?://[^/]*", "", url)
    return [w for w in re.split(r"[/?&=_.,:+-]+", path.lower()) if w]


def _http_client_args(binary: str, args: list[str]) -> str | None:
    """None when every argument is an allowed read-only flag, its value, or one http(s) URL."""
    allowed = HTTP_CLIENT_FLAGS[binary]
    urls, i = [], 0
    while i < len(args):
        arg = args[i]
        flag, has_value, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if flag in allowed["valued"]:
            if not has_value:
                i += 1
                value = args[i] if i < len(args) else ""
            if flag in ("-H", "--header") and value.split(":", 1)[0].strip().lower() not in SAFE_HEADERS:
                return f"{binary} may only send the headers {sorted(SAFE_HEADERS)}"
            if flag in ("-O", "--output-document") and value != "-":
                return f"{binary} may only write the response to stdout (-O -)"
        elif flag in allowed["plain"] or (
            re.fullmatch(r"-[A-Za-z]+", arg) and set(arg[1:]) <= allowed["short_combinable"]
        ):
            pass
        elif not arg.startswith("-"):
            urls.append(arg)
        else:
            return f"forbidden {binary} flag {arg} (only GET requests that print to stdout are allowed)"
        i += 1
    if len(urls) != 1 or not URL_RE.match(urls[0]):
        return f"{binary} needs exactly one http(s) URL"
    if set(_url_words(urls[0])) & FORBIDDEN_WORDS:
        return f"forbidden words in URL {sorted(set(_url_words(urls[0])) & FORBIDDEN_WORDS)}"
    return None


def validate_step(step: Any, resources: set[str] | None) -> str | None:
    """None when the step is acceptable, else the reason it is rejected."""
    if not isinstance(step, dict) or step.get("op") not in PRIMITIVES:
        return f"unknown op {step.get('op') if isinstance(step, dict) else step!r}"
    op = step["op"]
    if op == "k8s_get":
        res = str(step.get("resource") or "").lower().strip()
        if not res or res.split(".")[0] in FORBIDDEN_RESOURCES:
            return f"resource {res or '?'} not allowed"
        if resources is not None and res not in resources and res.split(".")[0] not in resources:
            return f"resource {res} is not served by this cluster"
        fields = step.get("fields") or []
        if not isinstance(fields, list) or len(fields) > 8 or not all(isinstance(f, str) for f in fields):
            return "fields must be a list of at most 8 dot paths"
    elif op == "promql":
        q = step.get("query")
        if not isinstance(q, str) or not q.strip() or len(q) > MAX_QUERY_CHARS:
            return f"query must be a PromQL string of at most {MAX_QUERY_CHARS} characters"
    elif op == "logs_grep":
        try:
            re.compile(str(step.get("pattern") or ""))
        except re.error as exc:
            return f"bad pattern: {exc}"
        if not step.get("pattern") or len(str(step["pattern"])) > 200:
            return "pattern must be a regex of at most 200 characters"
    elif op == "exec":
        cmd = step.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd) or len(cmd) > 12:
            return "command must be a list of at most 12 strings"
        binary = cmd[0].rsplit("/", 1)[-1]
        if binary not in READONLY_BINARIES:
            return f"binary {binary} is not on the read-only list"
        # argv goes to exec without a shell; `&` only ever appears in a URL's query
        if any(SHELL_CHARS.search(c) for c in cmd if not (binary in HTTP_CLIENT_FLAGS and URL_RE.match(c))):
            return "shell metacharacters are not allowed"
        if binary in HTTP_CLIENT_FLAGS:
            why = _http_client_args(binary, cmd[1:])
            if why:
                return why
        bad = sorted((set(_words(cmd[1:])) & FORBIDDEN_WORDS) | {a.split("=", 1)[0] for a in cmd[1:]} & FORBIDDEN_FLAGS)
        if bad:
            return f"forbidden arguments {bad}"
        if any(SENSITIVE_PATH.search(c) for c in cmd[1:]):
            return "the command reads credential material"
    elif op == "http_get":
        port, path = step.get("port"), str(step.get("path") or "")
        if not isinstance(port, int) or not 0 < port < 65536 or not URL_PATH_RE.match(path):
            return "http_get needs an integer port and a URL path starting with / (no spaces or quotes)"
        if set(_url_words(path)) & FORBIDDEN_WORDS:
            return f"forbidden words in path {sorted(set(_url_words(path)) & FORBIDDEN_WORDS)}"
    return None


def validate(spec: Any, resources: set[str] | None = None, taken: set[str] | None = None) -> tuple[Crafted | None, str]:
    """(Crafted, "") for a usable spec, else (None, reason)."""
    if not isinstance(spec, dict):
        return None, "spec is not an object"
    name = str(spec.get("name") or "").lower()
    if not NAME_RE.match(name):
        return None, f"bad name {name!r}"
    if taken and PREFIX + name in taken:
        return None, f"name {name} already used"
    reveals = str(spec.get("reveals") or "").strip()
    if not reveals or len(reveals) > 300:
        return None, "reveals must be one sentence"
    steps = spec.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return None, f"steps must be a list of 1..{MAX_STEPS}"
    for step in steps:
        why = validate_step(step, resources)
        if why:
            return None, why
    applies = spec.get("applies_to") if isinstance(spec.get("applies_to"), dict) else {}
    applies = {k: str(v).lower() for k, v in applies.items() if k in ("role", "image_contains") and v}
    return Crafted(name=name, reveals=reveals, steps=steps, applies_to=applies), ""


# --------------------------------------------------------------------------- running


def _placeholders(ctx: AgentContext, cid: str) -> dict[str, str]:
    """Values for a crafted step's placeholders on one component: namespace, name, a ready pod, pod regex and
    Service."""
    from clients.jev_agent.tools._common import _comp
    from clients.jev_agent.tools._podexec import _ready_pod

    comp = _comp(ctx, cid)
    pod, _ = _ready_pod(comp)
    services = [s.get("name") for s in comp.get("services") or [] if isinstance(s, dict) and s.get("name")]
    return {
        "namespace": comp.get("namespace") or "",
        "name": comp.get("name") or "",
        "pod": pod or "",
        "pod_regex": metrics.pod_regex(comp),
        "service": services[0] if services else comp.get("name") or "",
    }


def _fill(text: str, values: dict[str, str]) -> str:
    """`text` with each {placeholder} replaced by its value."""
    out = str(text)
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _dig(obj: Any, path: str) -> Any:
    """The value at a dotted path in a JSON object, mapped over lists; None where the path ends early."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = [c.get(part) if isinstance(c, dict) else None for c in cur]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _short(value: Any, n: int = 120) -> str:
    """A value as compact text (JSON unless it is a string), cut to n characters."""
    text = value if isinstance(value, str) else json.dumps(value, default=str, separators=(",", ":"))
    return collector.trim(text, n)


def _run_k8s_get(step: dict, values: dict[str, str]) -> list[str]:
    """A k8s_get step: the chosen fields of each object read (up to MAX_ITEMS)."""
    from clients.jev_agent.tools import _common

    res = str(step["resource"]).lower()
    name = _fill(step.get("name") or "", values).strip()
    namespaced = step.get("namespaced", True)
    args = (
        ["get", res] + ([name] if name else []) + (["-n", values["namespace"]] if namespaced else []) + ["-o", "json"]
    )
    data = _common._get_json(args)
    items = data.get("items") if "items" in data else [data]
    fields = step.get("fields") or ["status"]
    lines = []
    for item in items[:MAX_ITEMS]:
        meta = item.get("metadata") or {}
        shown = "; ".join(f"{f}={_short(_dig(item, f))}" for f in fields)
        lines.append(f"{res}/{meta.get('name')}: {shown}")
    if len(items) > MAX_ITEMS:
        lines.append(f"... {len(items) - MAX_ITEMS} more {res}")
    return lines or [f"no {res} found"]


def _run_promql(step: dict, values: dict[str, str], call: Any) -> list[str]:
    """A promql step: summaries of up to three series of the filled query over the metrics range."""
    query = _fill(step["query"], values)
    series = [s for s in metrics.parse_prom(call(f"({query}){metrics.RANGE}")) if s["points"]]
    lines = []
    for s in series[:3]:
        label = ",".join(f"{k}={v}" for k, v in s["labels"].items() if k not in ("__name__", "instance", "job"))[:80]
        lines.append(
            metrics.describe_summary(label or "value", metrics.summarize_series(s["points"]), metrics._fmt_num)
        )
    return lines or [f"no series for {collector.trim(query, 120)}"]


def _run_logs_grep(step: dict, values: dict[str, str]) -> list[str]:
    """A logs_grep step: how many of the pod's last 400 log lines match the pattern, and the last three matches."""
    if not values["pod"]:
        return ["no ready pod to read logs from"]
    args = ["logs", values["pod"], "-n", values["namespace"], "--all-containers=true", "--tail=400"]
    if step.get("previous"):
        args.append("--previous")
    text = collector.run_kubectl(args, timeout=30)
    rx = re.compile(str(step["pattern"]))
    hits = [ln for ln in text.splitlines() if rx.search(ln)]
    lines = [f"{len(hits)} of {len(text.splitlines())} log lines match /{step['pattern']}/"]
    lines += [collector.trim(h, 200) for h in hits[-3:]]
    return lines


def _run_exec(ctx: AgentContext, cid: str, step: dict, values: dict[str, str]) -> list[str]:
    """An exec step: the command's output from a ready pod of the component, or why it did not run (no ready pod, no
    memory headroom, exec failed, binary missing)."""
    from clients.jev_agent.tools import _podexec
    from clients.jev_agent.tools._common import _comp
    from clients.jev_agent.tools._podexec import ExecUnavailable, _ready_pod

    comp = _comp(ctx, cid)
    pod, container = _ready_pod(comp)
    if not pod:
        return [f"{cid} has no ready pod to run a command in"]
    cmd = [_fill(c, values) for c in step["command"]]
    binary = cmd[0].rsplit("/", 1)[-1]
    ok, why = probes.headroom_ok(comp, pod, container, READONLY_BINARIES[binary])
    if not ok:
        return [f"`{' '.join(cmd)}` skipped: {why}"]
    if binary.startswith("kafka-"):
        cmd = KAFKA_ENV + cmd
    try:
        rc, text = _podexec._exec(values["namespace"], pod, container, cmd)
    except ExecUnavailable as exc:
        return [f"`{' '.join(cmd)}` could not run: {collector.trim(str(exc), 120)}"]
    # kubectl's own trailer ("command terminated with exit code N") is not output of the command
    out = [ln for ln in (text or "").splitlines() if ln.strip() and not EXEC_TRAILER.match(ln.strip())]
    head = f"`{' '.join(step['command'])}` in {pod} (rc={rc})"
    if rc in (126, 127):  # the binary is not in the image: only the error line, never "output"
        return [head + (f": {collector.trim(out[0], 200)}" if out else ": command not found")]
    return [head + (f": {collector.trim(out[0], 200)}" if out else ": no output")] + [
        collector.trim(ln, 200) for ln in out[1 : MAX_FINDINGS - 1]
    ]


def _http_rows(head: str, text: str, step: dict) -> list[str]:
    """'<head>: N bytes' and the response lines (only those matching the step's grep, when it has one)."""
    rows = text.splitlines()
    if step.get("grep"):
        rx = re.compile(str(step["grep"]))
        rows = [r for r in rows if rx.search(r)]
    return [f"{head}: {len(text)} bytes"] + [collector.trim(r, 200) for r in rows[: MAX_FINDINGS - 1]]


def _target_port(comp: dict, service: str, port: int) -> int:
    """The container port a Service port forwards to (the same number when unknown or named)."""
    for svc in comp.get("services") or []:
        if not isinstance(svc, dict) or svc.get("name") != service:
            continue
        for spec in svc.get("ports") or []:
            m = re.match(r"^(\d+)->(\d+)/", str(spec))
            if m and int(m.group(1)) == port:
                return int(m.group(2))
    return port


def _http_in_pod(ctx: AgentContext, cid: str, step: dict, values: dict[str, str], path: str) -> list[str]:
    """GET from inside the application's network: the component's own pod on localhost first, then other ready pods
    of its namespace through the Service name, with whichever HTTP client their image has."""
    from clients.jev_agent.tools import _podexec
    from clients.jev_agent.tools._common import _comp
    from clients.jev_agent.tools._podexec import ExecUnavailable, _missing_tool
    from clients.jev_agent.tools.client_check import _check_sources

    comp = _comp(ctx, cid)
    ns, port = values["namespace"], int(step["port"])
    local = f"http://127.0.0.1:{_target_port(comp, values['service'], port)}{path}"
    remote = f"http://{values['service']}.{ns}.svc:{port}{path}"
    commands = {
        "curl": lambda url: ["curl", "-sS", "-m", HTTP_TIMEOUT_S, "-w", "\n[http %{http_code}]", url],
        "wget": lambda url: ["wget", "-q", "-O-", "-T", HTTP_TIMEOUT_S, url],
    }
    tried: list[str] = []
    attempts = 0
    for source_cid, pod, container in _check_sources(ctx, cid, cid):
        url = local if source_cid == cid else remote
        source = _comp(ctx, source_cid)
        for binary, build in commands.items():
            if attempts >= MAX_HTTP_ATTEMPTS:
                break
            ok, _ = probes.headroom_ok(source, pod, container, READONLY_BINARIES[binary])
            if not ok:
                continue
            attempts += 1
            try:
                rc, text = _podexec._exec(ns, pod, container, build(url))
            except ExecUnavailable:
                break
            if _missing_tool(rc, text or ""):
                continue
            if rc == 0:
                return _http_rows(f"GET {url} with {binary} in {pod}", text or "", step)
            tried.append(f"{binary} in {pod} rc={rc}: {collector.trim((text or '').strip() or 'no output', 80)}")
            break  # the client ran: another client in the same pod would see the same network
    return [
        f"GET {values['service']}:{port}{path} failed from inside the namespace: {'; '.join(tried) or 'no pod with curl or wget'}"
    ]


def _run_http_get(ctx: AgentContext, cid: str, step: dict, values: dict[str, str]) -> list[str]:
    """GET through the API server's service proxy; where that proxy is refused, from inside a pod instead."""
    path = _fill(step["path"], values)
    if not ctx.extra.get("service_proxy_refused"):
        raw = f"/api/v1/namespaces/{values['namespace']}/services/{values['service']}:{step['port']}/proxy{path}"
        try:
            text = collector.run_kubectl(["get", "--raw", raw], timeout=20)
            return _http_rows(f"GET {values['service']}:{step['port']}{path}", text, step)
        except Exception as exc:  # noqa: BLE001 - only a refused proxy falls back
            if "forbidden" not in str(exc).lower():
                raise
            ctx.extra["service_proxy_refused"] = True
    return _http_in_pod(ctx, cid, step, values, path)


def run(ctx: AgentContext, tool: Crafted, cid: str) -> ToolResult:
    """Run every step for one component; a failed step becomes a line, not an exception."""
    values = _placeholders(ctx, cid)
    call = ctx.extra.get("prom_call") or metrics.prom_call()
    lines: list[str] = []
    errors = 0
    for step in tool.steps:
        try:
            if step["op"] == "k8s_get":
                got = _run_k8s_get(step, values)
            elif step["op"] == "promql":
                got = _run_promql(step, values, call)
            elif step["op"] == "logs_grep":
                got = _run_logs_grep(step, values)
            elif step["op"] == "exec":
                got = _run_exec(ctx, cid, step, values)
            else:
                got = _run_http_get(ctx, cid, step, values)
        except Exception as exc:  # noqa: BLE001 - report the failed step and go on
            errors += 1
            got = [f"{step['op']} failed: {collector.trim(f'{type(exc).__name__}: {exc}', 160)}"]
        lines += got
    error = "all steps failed" if errors == len(tool.steps) else None
    return ToolResult(lines=lines[:MAX_FINDINGS], error=error, raw={"crafted": tool.to_dict()})


def applies(ctx: AgentContext, tool: Crafted, cid: str) -> bool:
    """Whether the tool can run on component `cid`: its role, image and name filters match, and an in-task tool only
    on its lead."""
    from clients.jev_agent.tools._common import _components

    comp = _components(ctx).get(cid)
    if comp is None or (tool.only_target and cid != tool.only_target):
        return False
    role = tool.applies_to.get("role")
    if role and (backend_role(comp) or "") != role:
        return False
    needle = tool.applies_to.get("image_contains")
    images = " ".join(str(c.get("image") or "") for c in comp.get("containers") or []).lower()
    return not needle or needle in images or needle in str(comp.get("name") or "").lower()


def register(registry: dict[str, ToolSpec], tool: Crafted) -> ToolSpec:
    """Add the tool to the menu (component targets)."""
    spec = ToolSpec(
        name=tool.tool_name,
        target_kind=COMPONENT,
        reveals=f"(written for this environment) {tool.reveals}",
        run=lambda ctx, cid, _t=tool: run(ctx, _t, cid),
        applicable=lambda ctx, cid, _t=tool: applies(ctx, _t, cid),
        uses_mcp=any(s.get("op") == "promql" for s in tool.steps),
    )
    registry[tool.tool_name] = spec
    return spec


def useful(result: ToolResult) -> bool:
    """A smoke-test result worth keeping: it ran and returned something other than failures or emptiness."""
    if result.error:
        return False
    bad = (
        "failed:",
        "could not run",
        "skipped:",
        "no series for",
        "no ready pod",
        "failed from inside the namespace",
        "not on the read-only",
        "(rc=126)",
        "(rc=127)",
        "no such file or directory",
        "executable file not found",
        "command not found",
        "command terminated with exit code",
    )
    return any(not any(b in ln.lower() for b in bad) for ln in result.lines)
