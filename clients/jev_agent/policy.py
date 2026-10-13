"""The policy loop: one decision request per step chooses the next generic evidence tool.

Each step sends the notebook state with five questions in ONE request:

* next_action (Choice): options are "tool:target" keys (plus "submit_now"), each described by what the tool
  reveals, the target, and what is already known about it;
* last_result_abnormal (Noul): about the most recent observation; the answer is stored on it;
* enough_evidence (Noul);
* origin (Choice): component ids + "cluster_object" + "not_yet_known";
* fault_category (Choice): classifier.FAULT_CATEGORIES.

Stopping (never before cfg.min_steps tool steps): enough_evidence >= cfg.enough_p and the top real origin (not
"not_yet_known") >= cfg.origin_p on two consecutive steps with the same origin, or >= cfg.origin_p_once once; or
submit_now chosen with p >= cfg.submit_p; or the same origin on cfg.stable_steps steps in a row (stable); or max
steps / wall budget (followed by one concluding request). The thresholds are AgentConfig settings.

The scripted policy (JEV_AGENT_POLICY=scripted) runs a fixed action order with the same tools and budget, and
asks the model for origin and fault_category once at the end.
"""

from __future__ import annotations

import importlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from typesafe_sdk import Choice, Noul

from clients.jev_agent import escalate as esc
from clients.jev_agent import evidence
from clients.jev_agent.config import POLICY_SCRIPTED, AgentConfig
from clients.jev_agent.notebook import Notebook, action_key, named_in, split_action
from clients.jev_agent.types import COMPONENT, EDGE, SCOPE, AgentContext, Observation, ToolResult
from clients.jev_diag.classifier import FAULT_CATEGORIES, ChoiceResult, JevDiagnoser, _sum_usage

logger = logging.getLogger("all.jev_agent.policy")

SUBMIT = "submit_now"
MENTION_BONUS = 100.0  # targets named by earlier observations sort ahead of abnormality scores
EXTRA_STEPS = 3  # one-time extension when the budget ends on a fresh abnormal finding
CLUSTER_OBJECT = "cluster_object"
NOT_YET_KNOWN = "not_yet_known"

Q_NEXT = "next_action"
Q_ABNORMAL = "last_result_abnormal"
Q_ENOUGH = "enough_evidence"
Q_ORIGIN = "origin"
Q_CATEGORY = "fault_category"
Q_ERROR_PREFIX = "error_line_"
MAX_ERROR_QUESTIONS = 4  # error lines of the last observation classified per step
ERROR_CLASS_P = 0.5  # a label is kept when its probability reaches this
# What an error line says went wrong, for the evidence kinds (evidence.py): the decision model reads the line, so
# no wording list decides it.
ERROR_CLASSES = {
    "authentication": "A login, credential, password, token, certificate or permission was rejected.",
    "data": "The data read or written is wrong: malformed, unparseable, corrupt, inconsistent, out of range, or "
    "violating a constraint or limit of the data store (a full sequence, a duplicate key, a missing record).",
    "configuration": "A setting of this component is wrong or missing: an invalid option, an unknown host or "
    "port, a missing file or variable.",
    "resource": "A resource ran out: memory, CPU, disk, file descriptors, connections, threads or a quota.",
    "connection": "Another component could not be reached or did not answer in time (refused, timeout, reset, "
    "no route, name not resolved).",
    "dependency": "Another component answered with an error: the failure is reported here but happened there.",
    "other": "None of the above, or not an error.",
}

CATEGORY_AFTER_ENOUGH = 0.5  # fault_category is asked once enough_evidence reached this (and when concluding)

# Dependency triage: when one component's errors name another, check both ends of that edge.
TRIAGE_EDGES = 2
TRIAGE_CALLEE_TOOLS = ("backend_probe", "logs", "metric_series_app", "describe")
FIRST_LOOK = 2  # the most abnormal components with ongoing errors or missing replicas: read their logs first
TRIAGE_CALLER_TOOLS = ("describe", "config_and_changes")
MAX_TRIAGE_OPTIONS = 4
LOGIN_CLIENT_TOOLS = ("config_and_changes", "describe")  # a client's credential sources and when they were written
MAX_STOP_DEFERRALS = 3
MAX_FORCED_CHECKS = 4  # checks a deferred stop runs before the model is asked again
OPENING_CHECKS = 2  # first looks run before the first decision

# LLM-crafted tools (discovery.craft_now): the menu offers CRAFT once this many tool steps were taken and the
# evidence is still short of enough.
CRAFT = "craft_tool"
CRAFT_MIN_STEPS = 3
CRAFT_ENOUGH_BELOW = 0.5

SCRIPTED_COMPONENT_TOOLS = (
    "describe",
    "logs",
    "metric_series_cpu",
    "metric_series_memory",
    "metric_series_rpc",
    "call_edges",
    "config_and_changes",
)
SCRIPTED_TOP_COMPONENTS = 3
SCRIPTED_CLUSTER_TOOL = "cluster_objects"


# --------------------------------------------------------------------------- decision requests


class DecisionAsker:
    """Sends decision requests through classifier.JevDiagnoser._ask, so requests and trace records have the
    same shape as jev_diag's (and render in the trajectory)."""

    def __init__(self, client: Any, trace: Any = None) -> None:
        """Wrap `client` in a JevDiagnoser, whose _ask sends the requests and records them on `trace`."""
        self._diag = JevDiagnoser(client=client, trace=trace)

    @property
    def requests(self) -> list[dict]:
        """Every Decisions request made so far."""
        return self._diag.requests

    def ask(self, label: str, state: dict, questions: dict) -> Any:
        """Ask the Decisions model one set of questions about `state`."""
        return self._diag._ask(label, state, questions)

    @property
    def calls(self) -> int:
        """Number of Decisions requests made."""
        return len(self.requests)

    @property
    def input_tokens(self) -> int:
        """Input tokens of all Decisions requests."""
        return _sum_usage("input_tokens", self.requests) or 0

    @property
    def output_tokens(self) -> int:
        """Output tokens of all Decisions requests."""
        return _sum_usage("output_tokens", self.requests) or 0


def _choice(response: Any, name: str) -> ChoiceResult | None:
    """The named choice answer of a decision response, or None when it has none."""
    choices = getattr(response, "choices", None) or {}
    return ChoiceResult.from_answer(choices[name]) if name in choices else None


def _noul(response: Any, name: str) -> float | None:
    """The named probability answer of a decision response, or None when it has none."""
    nouls = getattr(response, "nouls", None) or {}
    return float(nouls[name].noul) if name in nouls else None


def real_origin(result: ChoiceResult | None) -> tuple[str | None, float]:
    """Best origin other than not_yet_known, with its probability."""
    if result is None:
        return None, 0.0
    for value, p in result.ranked():
        if value != NOT_YET_KNOWN:
            return value, p
    return None, 0.0


# --------------------------------------------------------------------------- tool layer access


def _default_module(name: str) -> Any:
    """The jev_agent module `name` (tools or overview), imported on first use."""
    return importlib.import_module(f"clients.jev_agent.{name}")


def component_label(ctx: AgentContext, cid: str) -> str:
    """How a component is named in options: kind, name and namespace."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    comp = comps.get(cid) if isinstance(comps, dict) else None
    if isinstance(comp, dict) and comp.get("name"):
        return f"{comp.get('kind', 'workload')} `{comp['name']}` in namespace `{comp.get('namespace')}`"
    return f"workload `{cid}`"


def component_ids(ctx: AgentContext, scores: dict[str, float]) -> list[str]:
    """Every component id, the scored ones first."""
    ids = list(scores)
    comps = getattr(ctx.snapshot, "components", None) or {}
    if isinstance(comps, dict):
        ids += [c for c in comps if c not in scores]
    return list(dict.fromkeys(ids))


OBJECT_KINDS = {"service": "Service", "configmap": "ConfigMap", "secret": "Secret"}
MAX_OBJECT_ORIGINS = 40
FOCUS_TOP = 6


def _examined_components(notebook: Notebook, comps: dict) -> list[str]:
    """The components a done action targeted (either end of an edge), in sorted action order."""
    out: list[str] = []
    for key in sorted(notebook.actions_done):
        _, _, target = key.partition(":")
        for part in target.split("->"):
            part = part.strip()
            if part in comps:
                out.append(part)
    return out


def _consumes_messages(ctx: AgentContext, cid: str, comps: dict) -> bool:
    """Whether the component talks to a queue or stream backend (its input can then be a bad message). For an
    RPC server the "input" is its callers' requests, which the interaction origins cover."""
    from clients.jev_agent.roles import MESSAGING_ROLES, backend_role

    view = ((ctx.overview or {}).get("components") or {}).get(cid) or {}
    return any(
        isinstance(comps.get(dep), dict) and backend_role(comps[dep]) in MESSAGING_ROLES
        for dep in view.get("depends_on") or []
    )


def object_candidates(ctx: AgentContext, notebook: Notebook, scores: dict[str, float]) -> dict[str, str]:
    """Non-workload objects that can be named as the origin, found generically: the Services that select the
    examined or most abnormal components' pods, the ConfigMaps/Secrets those components reference, and objects
    the overview lists as changed shortly before the symptoms began. Ids are "<kind>/<namespace>/<name>"."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    if not isinstance(comps, dict):
        comps = {}
    # Most relevant first (named by observations, then examined components with abnormal findings, then by
    # abnormality score), so the cap never drops the components the evidence is about.
    named = named_targets(ctx, notebook)
    abnormal_targets = {
        part.strip() for o in notebook.observations if (o.abnormal or 0.0) >= 0.5 for part in o.target.split("->")
    }
    candidates = set(_examined_components(notebook, comps)) | set(sorted(scores, key=lambda c: -scores[c])[:FOCUS_TOP])
    focus = sorted(
        candidates,
        key=lambda c: (c not in named, c not in abnormal_targets, -scores.get(c, 0.0), c),
    )
    out: dict[str, str] = {}
    for cid in focus:
        comp = comps.get(cid)
        if not isinstance(comp, dict):
            continue
        ns = comp.get("namespace")
        for svc in comp.get("services") or []:
            name = svc.get("name") if isinstance(svc, dict) else None
            if name:
                out.setdefault(
                    f"service/{ns}/{name}",
                    f"Service `{name}` in namespace `{ns}` (its selector matches pods of {cid}) is where the fault "
                    "originates: its own spec (selector, ports, traffic settings) is the defect.",
                )
        if _consumes_messages(ctx, cid, comps):
            out.setdefault(
                f"data/{ns}/{comp.get('name')}",
                f"The messages {cid} consumes from its queue or stream are where the fault originates: the data "
                "is bad or unprocessable, not the component's code or configuration.",
            )
        refs = comp.get("config_refs") or {}
        for key, kind in (("configmaps", "configmap"), ("secrets", "secret")):
            for name in refs.get(key) or []:
                if name:
                    out.setdefault(
                        f"{kind}/{ns}/{name}",
                        f"{OBJECT_KINDS[kind]} `{name}` in namespace `{ns}` (used by {cid}) is where the fault "
                        "originates: its content is the defect.",
                    )
    # interactions: a component whose error lines name another (caller -> callee)
    for o in notebook.observations:
        for line in o.lines:
            if not str(line).startswith("error lines name other components:"):
                continue
            for callee in str(line).split(":", 1)[1].split(","):
                callee = callee.strip()
                a, b = comps.get(o.target), comps.get(callee)
                if isinstance(a, dict) and isinstance(b, dict):
                    out.setdefault(
                        f"interaction/{a.get('namespace')}/{a.get('name')}--{b.get('name')}",
                        f"The interaction between {o.target} (caller) and {callee} (callee) is where the fault "
                        "originates: the two sides' settings together (timeouts, retries, capacity, queueing, "
                        "protocol or credentials) cause it, rather than either component alone.",
                    )
    cluster = (ctx.overview or {}).get("cluster") or {}
    for change in cluster.get("recent_changes") or []:
        if not isinstance(change, dict) or not evidence.change_in_window(ctx, change):
            continue
        obj = change.get("object")
        if obj and obj not in comps and obj.count("/") >= 1:
            parts = obj.split("/")
            out.setdefault(
                obj,
                f"{parts[0]} `{parts[-1]}`"
                + (f" in namespace `{parts[1]}`" if len(parts) == 3 else "")
                + " (changed shortly before the symptoms began) is where the fault originates.",
            )
    return dict(list(out.items())[:MAX_OBJECT_ORIGINS])


def error_edges(notebook: Notebook) -> list[tuple[str, str]]:
    """(caller, callee) pairs from the logs tool's "error lines name other components" lines, newest first."""
    edges: list[tuple[str, str]] = []
    for o in reversed(notebook.observations):
        caller = o.target.split("->")[0].strip()
        for line in o.lines:
            for callee in named_in(line):
                if callee != caller and (caller, callee) not in edges:
                    edges.append((caller, callee))
    return edges


def named_targets(ctx: AgentContext, notebook: Notebook) -> set[str]:
    """Components that ongoing errors elsewhere name (structured links only, never incidental text such as an
    env var name or the tool's own wording) and that have not been examined yet."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    examined = {part.strip() for o in notebook.observations for part in o.target.split("->")}
    return {callee for _, callee in error_edges(notebook) if callee in comps and callee not in examined}


def triage_actions(
    notebook: Notebook, available: dict[str, list[str]], ctx: AgentContext | None = None
) -> list[tuple[str, str, str]]:
    """(tool, target, reason) checks on both ends of the newest error edges that are still available, and on the
    clients of a server that reports failed logins (no log names them)."""
    out: list[tuple[str, str, str]] = []
    for caller, callee in error_edges(notebook)[:TRIAGE_EDGES]:
        plan = [
            (TRIAGE_CALLEE_TOOLS[0], callee, f"{caller}'s errors name {callee}: does {callee} itself answer?"),
            (TRIAGE_CALLER_TOOLS[0], caller, f"{caller}'s errors name {callee}: {caller}'s own settings for it"),
            (TRIAGE_CALLEE_TOOLS[1], callee, f"{caller}'s errors name {callee}: {callee}'s own errors"),
            (TRIAGE_CALLEE_TOOLS[2], callee, f"{caller}'s errors name {callee}: is {callee} saturated or rejecting?"),
            (TRIAGE_CALLER_TOOLS[1], caller, f"{caller}'s errors name {callee}: changes on the caller side"),
            (TRIAGE_CALLEE_TOOLS[3], callee, f"{caller}'s errors name {callee}: {callee}'s state"),
            ("client_check", f"{caller}->{callee}", f"{caller}'s errors name {callee}: reachability from the caller"),
        ]
        for tool, target, reason in plan:
            if target in available.get(tool, ()) and (tool, target) not in [(t, g) for t, g, _ in out]:
                out.append((tool, target, reason))
    for server in evidence.login_failures(notebook) if ctx is not None else []:
        for client in login_clients(ctx, server):
            for tool in LOGIN_CLIENT_TOOLS:
                if client in available.get(tool, ()) and (tool, client) not in [(t, g) for t, g, _ in out]:
                    out.append((tool, client, f"{server} reports failed logins: {client}'s credential settings"))
    return out[: MAX_TRIAGE_OPTIONS + 2]


# Env var names that carry a credential or a connection string (the value is never read).
CREDENTIAL_ENV_RE = re.compile(
    r"(?i)pass(word)?|pwd|secret|token|credential|auth|conn(ection)?_?str|dsn|database_url|db_url"
)
MAX_LOGIN_CLIENTS = 3


def login_clients(ctx: AgentContext, server: str) -> list[str]:
    """Components that may present credentials to `server`: its known clients, and components in any of the
    application's namespaces whose env var names carry credentials or connection strings or that read a Secret
    (the dependency graph often misses clients whose connection string sits in a Secret, and a shared database
    often lives in a namespace of its own). Those changed shortly before the symptoms come first."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    view = (ctx.overview or {}).get("components") or {}
    namespaces = set(ctx.namespaces or []) | {(comps.get(server) or {}).get("namespace")}
    known = [c for c in (view.get(server) or {}).get("clients") or [] if c != server]
    likely = []
    for cid, comp in comps.items():
        if cid == server or not isinstance(comp, dict) or comp.get("namespace") not in namespaces:
            continue
        names = [n for c in comp.get("containers") or [] for n in c.get("env_names") or []]
        if any(CREDENTIAL_ENV_RE.search(n) for n in names) or (comp.get("config_refs") or {}).get("secrets"):
            likely.append(cid)
    changed = {
        ch.get("object")
        for ch in ((ctx.overview or {}).get("cluster") or {}).get("recent_changes") or []
        if isinstance(ch, dict) and evidence.change_in_window(ctx, ch)
    }
    ordered = list(dict.fromkeys(known + sorted(likely)))
    ordered.sort(key=lambda c: (c not in changed, c not in known))
    return ordered[:MAX_LOGIN_CLIENTS]


def first_look_actions(ctx: AgentContext, scores: dict[str, float], available: dict[str, list[str]]) -> list[str]:
    """The first reads for the FIRST_LOOK most abnormal components, by symptom: `describe` (pod states,
    scheduling and events) when ready replicas are missing, since a pod that never started has no logs; `logs`
    when they have ongoing error lines. Then, whatever its rank, `describe` of any workload whose ready pods still
    run an older pod template than the desired one: a paused or stuck rollout serves two configurations at once,
    which no error line or missing replica shows, and only describe shows how the two differ. Reads already done
    are left out."""
    comps = (ctx.overview or {}).get("components") or {}
    out = []
    for cid in sorted(scores, key=lambda c: -scores[c])[:FIRST_LOOK]:
        view = comps.get(cid) or {}
        rep = view.get("replicas") or {}
        if (rep.get("desired") or 0) > (rep.get("ready") or 0) and cid in available.get("describe", ()):
            out.append(action_key("describe", cid))
        if (view.get("errors") or {}).get("ongoing_lines") and cid in available.get("logs", ()):
            out.append(action_key("logs", cid))
    for cid in sorted(scores, key=lambda c: -scores[c]):
        key = action_key("describe", cid)
        if "updated" in ((comps.get(cid) or {}).get("replicas") or {}) and cid in available.get("describe", ()):
            if key not in out:
                out.append(key)
    return out


def first_look(ctx: AgentContext, scores: dict[str, float], available: dict[str, list[str]]) -> list[str]:
    """Components whose logs should be read before anything else: among the FIRST_LOOK most abnormal ones,
    those with ongoing error lines or missing ready replicas, logs not read yet."""
    comps = (ctx.overview or {}).get("components") or {}
    out = []
    for cid in sorted(scores, key=lambda c: -scores[c])[:FIRST_LOOK]:
        view = comps.get(cid) or {}
        rep = view.get("replicas") or {}
        ongoing = (view.get("errors") or {}).get("ongoing_lines") or 0
        missing = (rep.get("desired") or 0) > (rep.get("ready") or 0)
        if (ongoing or missing) and cid in available.get("logs", ()):
            out.append(cid)
    return out


def origin_entity(key: str | None) -> str | None:
    """The thing an origin key is about: a workload, its Service and its input data share their name; other
    objects and interactions are their own entity."""
    if not key or key in (NOT_YET_KNOWN, CLUSTER_OBJECT):
        return key
    parts = key.split("/")
    if len(parts) == 2:
        return parts[1]
    if len(parts) == 3 and parts[0] in ("service", "data"):
        return parts[2]
    return key


def entity_view(probs: dict[str, float]) -> tuple[str | None, float, str | None]:
    """(top entity, its summed probability, its most probable key), ignoring not_yet_known."""
    sums: dict[str, float] = {}
    best: dict[str, tuple[float, str]] = {}
    for key, p in (probs or {}).items():
        if key == NOT_YET_KNOWN:
            continue
        ent = origin_entity(key)
        sums[ent] = sums.get(ent, 0.0) + p
        if ent not in best or p > best[ent][0]:
            best[ent] = (p, key)
    if not sums:
        return None, 0.0, None
    ent = max(sums, key=sums.get)
    return ent, sums[ent], best[ent][1]


def target_score(spec: Any, target: str, scores: dict[str, float]) -> float:
    """How abnormal a tool's target is (an edge takes its more abnormal end; cluster groups score 0)."""
    if spec.target_kind == EDGE:
        a, _, b = target.partition("->")
        return max(scores.get(a.strip(), 0.0), scores.get(b.strip(), 0.0))
    if spec.target_kind in (COMPONENT, SCOPE):
        return scores.get(target, 0.0)
    return 0.0  # cluster groups keep the tool layer's order


def available_actions(ctx: AgentContext, tools: Any, done: set[str]) -> dict[str, list[str]]:
    """tool name -> applicable, not yet executed targets in the tool layer's order."""
    out: dict[str, list[str]] = {}
    for name, spec in tools.REGISTRY.items():
        if spec.uses_mcp and not ctx.mcp_ok:
            continue
        try:
            targets = list(tools.candidate_targets(ctx, spec))
        except Exception as exc:  # noqa: BLE001 - one broken tool must not stop the loop
            logger.warning("candidate_targets(%s) failed: %s", name, exc)
            continue
        keep = []
        for target in dict.fromkeys(targets):
            if action_key(name, target) in done:
                continue
            try:
                if not spec.applicable(ctx, target):
                    continue
            except Exception:  # noqa: BLE001
                continue
            keep.append(target)
        if keep:
            out[name] = keep
    return out


def build_options(
    ctx: AgentContext,
    notebook: Notebook,
    tools: Any,
    scores: dict[str, float],
    cfg: AgentConfig,
    suggested: list[dict] | None = None,
    craft_ok: bool = False,
) -> dict[str, str]:
    """Ordered {"tool:target": description} with LLM suggestions pinned first, then at most cfg.per_tool targets
    per tool, merged round-robin (each tool's best target first, ties by component score), capped at
    cfg.max_options, plus submit_now."""
    available = available_actions(ctx, tools, notebook.actions_done)
    options: dict[str, str] = {}

    def describe(name: str, target: str) -> str:
        # what each tool reveals is listed once in the question's instructions (tool_glossary)
        return f"Run {name} on {target}. Known about {target}: {notebook.known(target)}."

    for item in suggested or []:
        key = action_key(item["tool"], item["target"])
        if item["target"] in available.get(item["tool"], ()) and key not in options:
            reason = f" Reason: {item['reason']}" if item.get("reason") else ""
            options[key] = f"(suggested) {describe(item['tool'], item['target'])}{reason}"
    for key in first_look_actions(ctx, scores, available):
        tool, cid = split_action(key)
        why = "it is missing ready replicas" if tool == "describe" else "it has ongoing errors"
        if key not in options:
            options[key] = f"(first look) {describe(tool, cid)} Reason: {why}."
    for tool, target, reason in triage_actions(notebook, available, ctx):
        key = action_key(tool, target)
        if key not in options:
            options[key] = f"(dependency check) {describe(tool, target)} Reason: {reason}."

    # Components that ongoing errors elsewhere name, not yet examined, rank first even when they look healthy:
    # a lead that has not been followed yet beats raw abnormality.
    named = named_targets(ctx, notebook)
    ranked_scores = {cid: (MENTION_BONUS if cid in named else 0.0) + v for cid, v in scores.items()}
    per_tool: dict[str, list[tuple[float, int, str]]] = {}
    for name, targets in available.items():
        spec = tools.REGISTRY[name]
        ranked = sorted(
            ((target_score(spec, t, ranked_scores), i, t) for i, t in enumerate(targets)), key=lambda x: (-x[0], x[1])
        )
        per_tool[name] = [r for r in ranked if action_key(name, r[2]) not in options][: cfg.per_tool]
    tool_order = list(per_tool)
    candidates = []
    for name in tool_order:
        for rank, (score, _, target) in enumerate(per_tool[name]):
            candidates.append((rank, -score, tool_order.index(name), name, target))
    candidates.sort()
    for _, _, _, name, target in candidates:
        if len(options) >= cfg.max_options:
            break
        options[action_key(name, target)] = describe(name, target)
    if craft_ok:
        options[CRAFT] = (
            "Have an assistant write and run one new read-only check (Kubernetes reads including custom resources, "
            "PromQL, log search, a read-only client command, an HTTP status endpoint) for state the evidence points "
            "to but none of the listed actions can show."
        )
    options[SUBMIT] = (
        "Stop investigating and write the diagnosis now: the observations already identify the origin of the "
        "fault and its mechanism."
    )
    return options


# --------------------------------------------------------------------------- questions


def tool_glossary(tools: Any, options: dict[str, str] | None) -> str:
    """'name: what it reveals' for the tools that appear in the options, once each."""
    names = list(dict.fromkeys(split_action(k)[0] for k in options or {} if k != SUBMIT))
    items = []
    for name in names:
        spec = getattr(tools, "REGISTRY", {}).get(name)
        if spec is not None:
            items.append(f"{name}: {str(spec.reveals).strip().rstrip('.')}")
    return " Tools: " + "; ".join(items) + "." if items else ""


def error_lines_to_label(notebook: Notebook, obs: Observation) -> list[str]:
    """Error lines of a logs observation the decision model has not classified yet (ongoing errors, a crash's last
    words, repeated failures), at most MAX_ERROR_QUESTIONS."""
    out = []
    for line in obs.lines:
        if line.startswith(("ongoing", "previous run", "repeats", "start-up crash")) and line not in (
            notebook.error_labels
        ):
            out.append(line)
    return out[:MAX_ERROR_QUESTIONS]


def _next_action_question(options: dict[str, str], glossary: str) -> Choice:
    """The next-action question over the step's options."""
    return Choice(
        instructions="You are investigating a fault in the application described in `app`. `overview` "
        "summarises every component; `observations` are the results of the actions taken so far (newest "
        "last); `hypotheses`, when present, were proposed by an assistant and may be wrong. Choose the single "
        "next action that best narrows down which component or object is the origin of the fault: follow "
        "the current evidence (ongoing errors, missing replicas, components that errors name) rather than "
        "surveying healthy components. "
        "When observations name another component (for example a dependency in error lines), examine that "
        "component before running more tools on one that has already been examined several times; options "
        "marked (dependency check) look at both ends of such a link. "
        "Choose submit_now only when the observations already identify the origin and its mechanism." + glossary,
        criteria=options,
    )


def _origin_criteria(ctx: AgentContext, notebook: Notebook, scores: dict[str, float]) -> dict[str, str]:
    """The origin question's options (components, object candidates, any cluster object, not yet known) annotated
    with their evidence; records the non-workload candidates in ctx.extra["origin_objects"]."""
    origin_criteria = {
        cid: f"{component_label(ctx, cid)} is where the fault originates." for cid in component_ids(ctx, scores)
    }
    origin_criteria.update(object_candidates(ctx, notebook, scores))
    origin_criteria[CLUSTER_OBJECT] = (
        "A non-workload object not listed above is the origin: an admission webhook, a ResourceQuota or "
        "LimitRange, an RBAC role or binding, a NetworkPolicy, a node, cluster DNS, storage, or another namespace- "
        "or cluster-wide object."
    )
    origin_criteria[NOT_YET_KNOWN] = "The evidence does not yet point to an origin."
    origin_criteria = evidence.annotate(ctx, notebook, origin_criteria, skip={NOT_YET_KNOWN})
    comps = getattr(ctx.snapshot, "components", None) or {}
    ctx.extra["origin_objects"] = [
        k
        for k in origin_criteria
        if k not in comps
        and k not in (NOT_YET_KNOWN, CLUSTER_OBJECT)
        and k.count("/") in (1, 2)
        and not k.startswith(("data/", "interaction/"))
    ]
    return origin_criteria


def build_step_questions(
    ctx: AgentContext,
    notebook: Notebook,
    options: dict[str, str] | None,
    scores: dict[str, float],
    *,
    judge_last: bool = True,
    ask_category: bool = True,
    glossary: str = "",
) -> dict[str, Any]:
    """The questions of one step: the next action, whether the last result was abnormal, whether the evidence is
    enough, the origin, and the fault category when due."""
    questions: dict[str, Any] = {}
    if options:
        questions[Q_NEXT] = _next_action_question(options, glossary)
    last = notebook.last()
    if judge_last and last is not None and last.abnormal is None:
        questions[Q_ABNORMAL] = Noul(
            instructions=f"Does observation {last.id} ({last.tool} on {last.target}) in `observations` show an "
            "abnormal condition, error, or recent change that could be part of the fault?",
            criteria={
                "true": "It shows current errors or failures, ongoing restarts, saturation, rejections, a missing "
                "or mismatched object or value, or a change made shortly before the symptoms began.",
                "false": "It shows normal, healthy behaviour, nothing informative, or only history: errors marked "
                "stopped, start-up crashes or restarts since recovered, events from before pods became Ready, or "
                "revisions rolled out with the deployment itself.",
            },
        )
    if judge_last and last is not None and last.tool == "logs":
        for i, line in enumerate(error_lines_to_label(notebook, last)):
            questions[f"{Q_ERROR_PREFIX}{i}"] = Choice(
                instructions=f"What does this error line of {last.target} (observation {last.id}) say went wrong? "
                f"Line: {line}",
                criteria=ERROR_CLASSES,
            )
    questions[Q_ENOUGH] = Noul(
        instructions="Do the observations so far identify the origin of the fault and how it causes the failure "
        "well enough to write the diagnosis now?",
        criteria={
            "true": "The origin component or object and its mechanism are clear from the observations.",
            "false": "More evidence is needed to tell the origin apart from components that only show symptoms.",
        },
    )
    origin_criteria = _origin_criteria(ctx, notebook, scores)
    questions[Q_ORIGIN] = Choice(
        instructions=evidence.TRIAGE + " "
        "Which component or object is the origin of the fault: where the faulty change or defect "
        "is, not merely where errors surface? Components that fail only because something they depend on fails "
        "are not the origin. When a Service, ConfigMap or Secret was itself changed and that change explains the "
        "failure, choose that object rather than the workloads that use it. A component that is only saturated "
        "or slow, with no defect or change of its own, is usually where the symptom shows rather than the origin: "
        "look at what drives its load (for example how its callers call it) before choosing it. A pod setting that "
        "differs from its workload's own template was set when the pod was admitted: the object that set it (an "
        "admission webhook, a LimitRange) is then the origin rather than the workload. Objects created or "
        "revised while the "
        "application was deployed are not changes.",
        criteria=origin_criteria,
    )
    if ask_category:
        questions[Q_CATEGORY] = Choice(
            instructions="Assuming the most likely origin, which category describes the cause of the fault? Pods "
            "pending, not ready, restarting, or failing are symptoms; choose what made them so.",
            criteria=FAULT_CATEGORIES,
        )
    return questions


# --------------------------------------------------------------------------- the investigator


@dataclass
class InvestigationResult:
    """What the investigation concluded and what it cost: origin, probabilities, category, observations, steps and
    tokens."""

    origin: str | None
    origin_probs: dict[str, float]
    fault_category: str | None
    observations: list[Observation]
    steps: list[dict]
    decision_calls: int
    tokens: dict
    escalations: list[dict]
    stop_reason: str
    fault_category_probs: dict[str, float] = field(default_factory=dict)
    tool_steps: int = 0
    policy: str = "model"
    crafts: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        """Fields for the results json."""
        return {
            "policy": self.policy,
            "origin": self.origin,
            "origin_probs": dict(sorted(self.origin_probs.items(), key=lambda kv: -kv[1])[:8]),
            "fault_category": self.fault_category,
            "steps": self.tool_steps,
            "decision_calls": self.decision_calls,
            "input_tokens": self.tokens.get("input_tokens", 0),
            "output_tokens": self.tokens.get("output_tokens", 0),
            "escalated": bool(self.escalations),
            "escalations": self.escalations,
            "crafts": self.crafts,
            "stop_reason": self.stop_reason,
            "step_trace": [
                {
                    k: s.get(k)
                    for k in (
                        "step",
                        "options",
                        "chosen",
                        "chosen_p",
                        "chosen_rank",
                        "executed",
                        "origin",
                        "origin_p",
                        "enough",
                        "last_abnormal",
                        "escalation",
                        "compression",
                    )
                    if k in s
                }
                for s in self.steps
            ],
        }


class Investigator:
    """The investigation loop: each step the decision model picks the next check, judges the last result and says
    whether to stop."""

    def __init__(
        self,
        ctx: AgentContext,
        client: Any,
        notebook: Notebook,
        trace: Any = None,
        llm: Any = None,
        *,
        config: AgentConfig | None = None,
        tools: Any = None,
        overview: Any = None,
        scores: dict[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Set up the loop: the decision client is wrapped in a DecisionAsker unless it is one; config, tools,
        overview and scores default to the loaded config, the jev_agent modules and the notebook's scores."""
        from clients.jev_agent.config import load

        self.ctx = ctx
        self.cfg = config or load()
        self.notebook = notebook
        self.trace = trace
        self.llm = llm
        self.asker = client if isinstance(client, DecisionAsker) else DecisionAsker(client, trace)
        self.tools = tools if tools is not None else _default_module("tools")
        self._overview_mod = overview
        if scores is None:
            scores = notebook.scores or self._overview().component_scores(ctx.overview)
        self.scores = dict(scores)
        if not notebook.scores:
            notebook.scores = dict(self.scores)
        self.clock = clock
        self.steps: list[dict] = []
        self.escalations: list[dict] = []
        self.suggested: list[dict] = []
        self.tool_steps = 0
        self._deferrals = 0
        self.crafts: list[dict] = []
        self._started = 0.0

    def _overview(self) -> Any:
        """The overview module (imported on first use unless one was given)."""
        if self._overview_mod is None:
            self._overview_mod = _default_module("overview")
        return self._overview_mod

    def _record(self, event: str, **payload: Any) -> None:
        """Record an agent event on the trace, when there is one."""
        if self.trace is not None:
            self.trace.record("agent", event, **payload)

    def _elapsed(self) -> float:
        """Seconds since the investigation started."""
        return self.clock() - self._started

    def _budget_left(self) -> bool:
        """Whether tool steps and wall-clock time remain."""
        return self.tool_steps < self.cfg.max_steps and self._elapsed() < self.cfg.wall_budget_s

    def _extend(self, stop: str, s: dict) -> bool:
        """Once per run, grant EXTRA_STEPS more steps when the step budget runs out right after a new abnormal
        finding and the origin is still unclear (never past the wall-clock budget)."""
        if stop != "max_steps" or getattr(self, "_extended", False):
            return False
        if self._elapsed() >= self.cfg.wall_budget_s:
            return False
        fresh = (s.get("last_abnormal") or 0.0) >= 0.5
        unsure = (s.get("origin_real_p") or 0.0) < self.cfg.origin_p or (s.get("enough") or 0.0) < self.cfg.enough_p
        if not (fresh and unsure):
            return False
        self._extended = True
        self.cfg.max_steps += EXTRA_STEPS
        self._record("budget_extended", extra=EXTRA_STEPS, max_steps=self.cfg.max_steps)
        return True

    def _llm_allowed(self) -> bool:
        """Whether an LLM call may be spent now, keeping one for the report."""
        if self.llm is None:
            return False
        # keep one call for the report write-up
        return getattr(self.llm, "calls", 0) < self.cfg.max_llm_calls - 1

    # ------------------------------------------------------------------ actions

    def execute(self, key: str) -> Observation:
        """Run one menu action and record its result (a failing tool becomes an observation with the error)."""
        tool, target = split_action(key)
        started = time.monotonic()
        try:
            result = self.tools.run(self.ctx, tool, target)
        except Exception as exc:  # noqa: BLE001 - a failed tool is an observation, not a crash
            result = ToolResult(lines=[], error=f"{type(exc).__name__}: {exc}")
        self.tool_steps += 1
        obs = self.notebook.add(self.tool_steps, tool, target, result)
        self._record(
            "tool",
            step=self.tool_steps,
            tool=tool,
            target=target,
            lines=obs.lines,
            error=obs.error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return obs

    # ------------------------------------------------------------------ model policy

    def _should_stop(self, s: dict, prev: dict | None) -> str | None:
        """The stop rule that step `s` (after `prev`) meets, or None; thresholds come from self.cfg."""
        c = self.cfg
        if self.tool_steps < self.cfg.min_steps:
            return None
        if s.get("chosen") == SUBMIT and (s.get("chosen_p") or 0.0) >= c.submit_p:
            return "submit_now"
        if s.get("chosen") == SUBMIT and prev is not None and prev.get("chosen") == SUBMIT:
            return "submit_twice"  # the model's top choice was to stop on two consecutive steps
        enough = s.get("enough") or 0.0
        entity, entity_p = s.get("entity"), s.get("entity_p") or 0.0
        if enough >= c.enough_p and entity not in (None, NOT_YET_KNOWN):
            if entity_p >= c.origin_p_once:
                return "confident_once"
            if (
                entity_p >= c.origin_p
                and prev is not None
                and (prev.get("enough") or 0.0) >= c.enough_p
                and prev.get("entity") == entity
                and (prev.get("entity_p") or 0.0) >= c.origin_p
            ):
                return "confident_twice"
        recent = self.steps[-c.stable_steps :]
        if (
            entity not in (None, NOT_YET_KNOWN)
            and len(recent) == c.stable_steps
            and all(
                (x.get("enough") or 0.0) >= c.stable_enough_p
                and x.get("entity") == entity
                and (x.get("entity_p") or 0.0) >= c.stable_origin_p
                for x in recent
            )
        ):
            return "stable"
        return None

    def _object_unseen(self, s: dict) -> list[str]:
        """object_view on the chosen origin when it is an object whose content no observation has shown."""
        key = s.get("origin_real")
        comps = getattr(self.ctx.snapshot, "components", None) or {}
        if (
            not key
            or key in comps
            or key in (NOT_YET_KNOWN, CLUSTER_OBJECT)
            or key.startswith(("data/", "interaction/"))
        ):
            return []
        if "object_view" not in getattr(self.tools, "REGISTRY", {}):
            return []
        action = action_key("object_view", key)
        if action in self.notebook.actions_done:
            return []
        available = available_actions(self.ctx, self.tools, self.notebook.actions_done)
        return [action] if key in available.get("object_view", ()) else []

    def _run_pending(self, keys: list[str]) -> list[str]:
        """Execute up to MAX_FORCED_CHECKS of the still-available checks that held a stop back."""
        ran: list[str] = []
        for key in keys:
            if len(ran) >= MAX_FORCED_CHECKS or self.tool_steps >= self.cfg.max_steps:
                break
            available = available_actions(self.ctx, self.tools, self.notebook.actions_done)
            tool, target = split_action(key)
            if target in available.get(tool, ()):
                self.execute(key)
                ran.append(key)
        return ran

    def _unread_failures(self) -> list[str]:
        """Reads a conclusion must not skip: the first looks (logs or describe of the most abnormal components that are
        failing, describe of a workload serving two pod templates),
        the credential settings of the clients of a server that reports failed logins, and the logs of a consumer
        reported stuck on one offset (only its own errors tell bad input from a wedged consumer)."""
        available = available_actions(self.ctx, self.tools, self.notebook.actions_done)
        pending = first_look_actions(self.ctx, self.scores, available)
        done = self.notebook.actions_done
        for server in evidence.login_failures(self.notebook):
            for client in login_clients(self.ctx, server):
                if any(action_key(t, client) in done for t in LOGIN_CLIENT_TOOLS):
                    continue
                pending += [action_key(t, client) for t in LOGIN_CLIENT_TOOLS if client in available.get(t, ())][:1]
        for consumer in evidence.stalled_consumers(self.notebook):
            if consumer in available.get("logs", ()) and action_key("logs", consumer) not in done:
                pending.append(action_key("logs", consumer))
        return pending

    def _triage_pending(self, s: dict) -> list[str]:
        """Dependency checks still open on an error edge that touches the current top origin."""
        entity = s.get("entity")
        if entity in (None, NOT_YET_KNOWN):
            return []
        comps = getattr(self.ctx.snapshot, "components", None) or {}
        touching = [
            (a, b)
            for a, b in error_edges(self.notebook)
            if entity in ((comps.get(a) or {}).get("name"), (comps.get(b) or {}).get("name"))
        ]
        if not touching:
            return []
        done = self.notebook.actions_done
        available = available_actions(self.ctx, self.tools, done)
        pending = []
        for caller, callee in touching[:TRIAGE_EDGES]:
            caller_seen = any(action_key(t, caller) in done for t in TRIAGE_CALLER_TOOLS)
            callee_seen = any(action_key(t, callee) in done for t in TRIAGE_CALLEE_TOOLS)
            if not caller_seen:
                pending += [action_key(t, caller) for t in TRIAGE_CALLER_TOOLS if caller in available.get(t, ())][:1]
            if not callee_seen:
                pending += [action_key(t, callee) for t in TRIAGE_CALLEE_TOOLS if callee in available.get(t, ())][:1]
        return pending

    def _decide(self, label: str, options: dict[str, str] | None) -> tuple[Any, dict]:
        """(response, step record) of one decision request: asks the step's questions on the current state, writes
        the abnormal judgement and error-line labels back to the notebook, and records the origin, category and
        chosen action."""
        state = self.notebook.state(self.cfg.state_budget)
        state["budget"] = {
            "steps_taken": self.tool_steps,
            "max_steps": self.cfg.max_steps,
            "steps_left": max(0, self.cfg.max_steps - self.tool_steps),
        }
        prev_enough = (self.steps[-1].get("enough") or 0.0) if self.steps else 0.0
        questions = build_step_questions(
            self.ctx,
            self.notebook,
            options,
            self.scores,
            ask_category=options is None or prev_enough >= CATEGORY_AFTER_ENOUGH,
            glossary=tool_glossary(self.tools, options),
        )
        last = self.notebook.last()
        judged = last if Q_ABNORMAL in questions else None
        response = self.asker.ask(label, state, questions)
        nxt = _choice(response, Q_NEXT)
        origin = _choice(response, Q_ORIGIN)
        category = _choice(response, Q_CATEGORY)
        abnormal = _noul(response, Q_ABNORMAL)
        if judged is not None and abnormal is not None:
            judged.abnormal = abnormal
        if judged is not None and judged.tool == "logs":
            lines = error_lines_to_label(self.notebook, judged)
            for i, line in enumerate(lines):
                klass = _choice(response, f"{Q_ERROR_PREFIX}{i}")
                if klass is not None:
                    p = klass.probabilities.get(klass.choice, klass.confidence) or 0.0
                    self.notebook.error_labels[line] = klass.choice if p >= ERROR_CLASS_P else "other"
        o_real, o_real_p = real_origin(origin)
        entity, entity_p, entity_key = entity_view(origin.probabilities if origin else {})
        if entity_key is not None:
            # report the most probable key of the most probable entity (its keys split one thing's probability)
            o_real, o_real_p = entity_key, (origin.probabilities.get(entity_key) or 0.0)
        s: dict[str, Any] = {
            "step": len(self.steps) + 1,
            "entity": entity,
            "entity_p": round(entity_p, 4),
            "label": label,
            "tool_steps": self.tool_steps,
            "compression": list(self.notebook.last_compression),
            "enough": _noul(response, Q_ENOUGH),
            "last_abnormal": abnormal if judged is not None else None,
            "judged": judged.id if judged is not None else None,
            "origin": origin.choice if origin else None,
            "origin_p": origin.probabilities.get(origin.choice) if origin else None,
            "origin_real": o_real,
            "origin_real_p": o_real_p,
            "origin_probs": origin.probabilities if origin else {},
            "category": category.choice if category else None,
            "category_probs": category.probabilities if category else {},
        }
        if options is not None:
            keys = list(options)
            s["options"] = len(keys)
            if nxt is not None:
                s["chosen"] = nxt.choice
                s["chosen_p"] = nxt.probabilities.get(nxt.choice, nxt.confidence)
                s["chosen_rank"] = keys.index(nxt.choice) + 1 if nxt.choice in keys else None
                s["next_p"] = max(nxt.probabilities.values()) if nxt.probabilities else nxt.confidence
                s["next_entropy"] = round(esc.normalized_entropy(nxt.probabilities), 4) if nxt.probabilities else None
                s["next_ranked"] = nxt.ranked()
        return response, s

    def _craft_ok(self) -> bool:
        """Whether the CRAFT option may be offered: crafting on, an LLM, crafts left, enough steps taken, and the
        evidence not yet near enough."""
        if not self.cfg.craft or self.llm is None or len(self.crafts) >= self.cfg.max_craft:
            return False
        if self.tool_steps < CRAFT_MIN_STEPS:
            return False
        last = self.steps[-1] if self.steps else {}
        return (last.get("enough") or 0.0) < CRAFT_ENOUGH_BELOW

    def _craft(self) -> None:
        """One LLM call: a new read-only tool for the current lead, registered and run at once."""
        from clients.jev_agent import discovery

        res = discovery.craft_now(self.llm, self.ctx, self.tools.REGISTRY, self.notebook.state(self.cfg.state_budget))
        info = {
            "tool": res.tool,
            "target": res.target,
            "reason": res.reason,
            "error": res.error,
            "spec": res.spec,
            "raw_text": res.raw_text,
        }
        self.crafts.append(info)
        self._record("craft", **info)
        if res.result is None:
            return
        self.tool_steps += 1
        obs = self.notebook.add(self.tool_steps, res.tool, res.target, res.result)
        self._record(
            "tool",
            step=self.tool_steps,
            tool=res.tool,
            target=res.target,
            lines=obs.lines,
            error=obs.error,
            crafted=True,
        )

    def _pick_action(self, s: dict, options: dict[str, str]) -> str | None:
        """The option to execute: the chosen one, or the most probable non-submit option."""
        chosen = s.get("chosen")
        if chosen and chosen != SUBMIT and chosen in options:
            return chosen
        for key, _ in s.get("next_ranked") or []:
            if key != SUBMIT and key in options:
                return key
        return next((k for k in options if k != SUBMIT), None)

    def _maybe_escalate(self) -> None:
        """Ask the LLM once for hypotheses and suggested actions, when the escalation trigger fires and the LLM
        budget allows."""
        if not self.cfg.escalate or self.escalations or not self._llm_allowed():
            return
        reason = esc.trigger(self.steps, tool_steps=self.tool_steps, max_steps=self.cfg.max_steps)
        if reason is None:
            return
        available = available_actions(self.ctx, self.tools, self.notebook.actions_done)
        menu = {name: set(targets) for name, targets in available.items()}
        result = esc.escalate(
            self.llm,
            reason=reason,
            notebook_state=self.notebook.state(self.cfg.state_budget),
            registry=self.tools.REGISTRY,
            menu=menu,
            observation_ids={o.id for o in self.notebook.observations},
            done=set(self.notebook.actions_done),
        )
        if result.hypotheses:
            self.notebook.set_hypotheses(result.hypotheses)
        self.suggested = result.suggested
        info = {"step": len(self.steps), **result.to_dict()}
        self.escalations.append(info)
        self.steps[-1]["escalation"] = reason
        self._record("escalation", **info)

    def _run_model(self) -> tuple[str, dict]:
        """The model policy: the opening checks, then one decision per step until a stop rule, the budget or the
        options run out; returns (stop reason, last step)."""
        prev: dict | None = None
        last: dict = {}
        # Open as an SRE would: the first looks at the most abnormal components (describe one missing replicas, read
        # the logs of one with ongoing errors) run before the model chooses, so no run starts somewhere else.
        available = available_actions(self.ctx, self.tools, self.notebook.actions_done)
        opening = self._run_pending(first_look_actions(self.ctx, self.scores, available)[:OPENING_CHECKS])
        if opening:
            self._record("opening", checks=opening)
        while True:
            if not self._budget_left():
                stop = "max_steps" if self.tool_steps >= self.cfg.max_steps else "wall_budget"
                if last and last.get("tool_steps") == self.tool_steps:
                    return stop, last  # no new observation since the last answer
                _, last = self._decide(f"conclude_{len(self.steps) + 1}", None)
                self.steps.append(last)
                if self._extend(stop, last):
                    continue
                return stop, last
            self.suggested = [
                x for x in self.suggested if action_key(x["tool"], x["target"]) not in self.notebook.actions_done
            ]
            options = build_options(
                self.ctx, self.notebook, self.tools, self.scores, self.cfg, self.suggested, craft_ok=self._craft_ok()
            )
            if len(options) <= 1 and self.tool_steps >= self.cfg.min_steps:
                if last and last.get("tool_steps") == self.tool_steps:
                    return "no_options", last
                _, last = self._decide(f"conclude_{len(self.steps) + 1}", None)
                self.steps.append(last)
                return "no_options", last
            _, s = self._decide(f"step_{len(self.steps) + 1}", options)
            self.steps.append(s)
            stop = self._should_stop(s, prev)
            if stop and stop != "submit_now":
                pending = list(
                    dict.fromkeys(self._object_unseen(s) + self._triage_pending(s) + self._unread_failures())
                )
                if pending and self._deferrals < MAX_STOP_DEFERRALS:
                    self._deferrals += 1
                    s["stop_deferred"] = {"stop": stop, "pending": pending}
                    self._record("stop_deferred", step=len(self.steps), stop=stop, pending=pending)
                    for key in pending:
                        tool, target = split_action(key)
                        if not any(action_key(x["tool"], x["target"]) == key for x in self.suggested):
                            self.suggested.append(
                                {"tool": tool, "target": target, "reason": "check this end of the error link first"}
                            )
                    stop = None
            if stop:
                return stop, s
            prev, last = s, s
            # a deferred stop runs the open checks itself: the model wanted to stop, not to look elsewhere
            forced = self._run_pending((s.get("stop_deferred") or {}).get("pending") or [])
            if forced:
                s["executed"] = forced[0]
                s["forced"] = forced
                continue
            self._maybe_escalate()
            key = self._pick_action(s, options)
            if key is None:
                return "no_options", s
            s["executed"] = key
            if key == CRAFT:
                self._craft()
            else:
                self.execute(key)

    # ------------------------------------------------------------------ scripted policy

    def scripted_plan(self) -> list[str]:
        """The fixed check order of the scripted baseline: the top components' tools, then the cluster objects."""
        available = available_actions(self.ctx, self.tools, set())
        top = [cid for cid, _ in sorted(self.scores.items(), key=lambda kv: -kv[1])[:SCRIPTED_TOP_COMPONENTS]]
        plan: list[str] = []
        for cid in top:
            for name in SCRIPTED_COMPONENT_TOOLS:
                targets = available.get(name) or []
                if cid in targets:
                    plan.append(action_key(name, cid))
                    continue
                spec = self.tools.REGISTRY.get(name)
                if spec is not None and spec.target_kind == EDGE:
                    edge = next((t for t in targets if cid in [p.strip() for p in t.split("->")]), None)
                    if edge:
                        plan.append(action_key(name, edge))
        for target in available.get(SCRIPTED_CLUSTER_TOOL) or []:
            plan.append(action_key(SCRIPTED_CLUSTER_TOOL, target))
        return list(dict.fromkeys(plan))

    def _run_scripted(self) -> tuple[str, dict]:
        """The scripted policy: run the fixed plan within the budget, then ask the decision model once for the origin
        and category; returns (stop reason, last step)."""
        plan = self.scripted_plan()
        self._record("scripted_plan", plan=plan)
        stop = "plan_done"
        for key in plan:
            if not self._budget_left():
                stop = "max_steps" if self.tool_steps >= self.cfg.max_steps else "wall_budget"
                break
            if key in self.notebook.actions_done:
                continue
            self.execute(key)
            self.steps.append({"step": len(self.steps) + 1, "executed": key, "tool_steps": self.tool_steps})
        state = self.notebook.state(self.cfg.state_budget)
        questions = build_step_questions(self.ctx, self.notebook, None, self.scores, judge_last=False)
        questions = {k: v for k, v in questions.items() if k in (Q_ORIGIN, Q_CATEGORY)}
        response = self.asker.ask("conclude", state, questions)
        origin = _choice(response, Q_ORIGIN)
        category = _choice(response, Q_CATEGORY)
        o_real, o_real_p = real_origin(origin)
        last = {
            "step": len(self.steps) + 1,
            "label": "conclude",
            "tool_steps": self.tool_steps,
            "origin": origin.choice if origin else None,
            "origin_p": origin.probabilities.get(origin.choice) if origin else None,
            "origin_real": o_real,
            "origin_real_p": o_real_p,
            "origin_probs": origin.probabilities if origin else {},
            "category": category.choice if category else None,
            "category_probs": category.probabilities if category else {},
        }
        self.steps.append(last)
        return stop, last

    def _ask_category(self, last: dict) -> None:
        """fault_category is only asked near the end; ask it once if the run stopped before that."""
        questions = build_step_questions(self.ctx, self.notebook, None, self.scores, judge_last=False)
        questions = {k: v for k, v in questions.items() if k == Q_CATEGORY}
        try:
            response = self.asker.ask("category", self.notebook.state(self.cfg.state_budget), questions)
        except Exception as exc:  # noqa: BLE001 - the report falls back to no category
            logger.warning("category request failed: %s", exc)
            return
        category = _choice(response, Q_CATEGORY)
        if category is not None:
            last["category"] = category.choice
            last["category_probs"] = category.probabilities

    # ------------------------------------------------------------------ entry point

    def run(self) -> InvestigationResult:
        """Investigate until a stop rule fires and return the conclusion."""
        self._started = self.clock()
        if self.cfg.policy == POLICY_SCRIPTED:
            stop, last = self._run_scripted()
        else:
            stop, last = self._run_model()
            if last.get("category") is None:
                self._ask_category(last)
        origin = last.get("origin_real")
        if origin is None and self.scores:
            origin = max(self.scores, key=self.scores.get)  # nothing chosen: the highest-scored component
        result = InvestigationResult(
            origin=origin,
            origin_probs=dict(last.get("origin_probs") or {}),
            fault_category=last.get("category"),
            fault_category_probs=dict(last.get("category_probs") or {}),
            observations=list(self.notebook.observations),
            steps=self.steps,
            decision_calls=self.asker.calls,
            tokens={"input_tokens": self.asker.input_tokens, "output_tokens": self.asker.output_tokens},
            escalations=self.escalations,
            stop_reason=stop,
            tool_steps=self.tool_steps,
            policy=self.cfg.policy,
            crafts=list(self.crafts),
        )
        self._record("conclusion", **{k: v for k, v in result.summary().items() if k != "step_trace"})
        return result
