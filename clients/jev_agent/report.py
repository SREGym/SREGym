"""The submitted diagnosis: evidence selection (one decision request) and a short write-up.

1. Evidence: one decision request with a Noul "supports_<Oid>" per observation (at most MAX_EVIDENCE_QUESTIONS,
   abnormal ones first): does this observation support the concluded origin and mechanism?
2. Write-up: an LLM turns origin, fault category gloss, the selected observation lines, and the affected
   neighbours into 120-200 words, using only those facts. Without an LLM, or on any error, a template in the
   style of classifier.build_diagnosis_text is used.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from typesafe_sdk import Noul

from clients.jev_agent import roles
from clients.jev_agent.notebook import Notebook
from clients.jev_agent.policy import CLUSTER_OBJECT, InvestigationResult, error_edges, origin_entity
from clients.jev_agent.types import AgentContext, Observation
from clients.jev_diag.classifier import CATEGORY_GLOSS

logger = logging.getLogger("all.jev_agent.report")

MAX_EVIDENCE_QUESTIONS = 15
MAX_EVIDENCE_LINES = 8
SUPPORT_P = 0.5
MIN_WORDS = 60

WRITEUP_SYSTEM = (
    "You write the root-cause diagnosis for a Kubernetes incident. Use only the facts given in the JSON; do not "
    "invent object names, values, events or numbers. Write 120 to 200 words of plain prose (no headings, no "
    "bullet lists). Name the faulty object and its namespace, explain the mechanism (how it causes the "
    "failure), and state the blast radius (which other components are affected and how). The faulty object is "
    "the one given in `origin` (by kind, name and namespace, and its Services when listed); describe it as the "
    "root cause and treat workloads "
    "that merely use or match it as affected. If the origin is a cluster-level object, name it exactly as written "
    "in the evidence lines. Use `propagation` (which component's errors name which other component) to explain "
    "how the failure spreads, and mention settings or conditions on neighbouring components that the evidence "
    "shows contributing to it. When `origin.framing` is `object_acting_on`, open with the object as the root cause "
    "acting on the workloads in `origin.acts_on`. When it is `targeted_component`, open with the workload in "
    "`origin.acts_on` as the root-cause component, and describe the change to the `origin` object as how the "
    "fault is applied to it. When the evidence shows the fault in the data a backend holds rather than in any "
    "Kubernetes object (a table, sequence, index, topic, queue, key, file or record named in error lines that is "
    "exhausted, corrupt, full, malformed or violating a constraint), name that data object, as the error lines name "
    "it, as the root cause and the `origin` workload as where it lives; this does not apply to failures to reach or "
    "log in to the backend."
)


@dataclass
class Report:
    """The submitted diagnosis: its text, where it came from, and the evidence behind it."""

    text: str
    source: str  # "llm" | "template"
    evidence: list[str] = field(default_factory=list)
    support: dict[str, float] = field(default_factory=dict)
    affected: list[str] = field(default_factory=list)
    llm_error: str | None = None
    evidence_error: str | None = None


def obs_line(o: Observation, max_lines: int = 6) -> str:
    """An observation as one line of evidence."""
    body = "; ".join(o.lines[:max_lines]) if o.lines else (o.error or "no findings")
    return f"{o.id} {o.tool} {o.target}: {body}"


def origin_facts(ctx: AgentContext, origin: str | None) -> dict:
    """What is known about the origin: its kind, name, namespace and Services, or a note when it is not a workload."""
    if not origin:
        return {"id": None, "note": "origin not determined"}
    if origin == CLUSTER_OBJECT:
        return {"id": CLUSTER_OBJECT, "note": "a non-workload (cluster-level) object; see the evidence lines"}
    comps = getattr(ctx.snapshot, "components", None) or {}
    comp = comps.get(origin) if isinstance(comps, dict) else None
    facts = {"id": origin}
    parts = origin.split("/")
    if comp is None and len(parts) == 3:  # non-workload object "<kind>/<namespace>/<name>"
        if parts[0] == "interaction":
            caller_name, _, callee_name = parts[2].partition("--")
            by_name = {c.get("name"): cid for cid, c in comps.items() if isinstance(c, dict)}
            pair = [by_name.get(caller_name), by_name.get(callee_name)]
            facts.update({"kind": "interaction", "namespace": parts[1], "name": f"{caller_name} -> {callee_name}"})
            facts["components"] = [c for c in pair if c]
            facts["used_by"] = [c for c in pair if c]
            facts["note"] = (
                "explain how the caller's settings and the callee's settings act together (for example timeouts, "
                "retries, queueing, capacity) to cause and sustain the failure; name both components"
            )
            return facts
        if parts[0] == "data":
            consumer = next(
                (cid for cid, c in comps.items() if isinstance(c, dict) and c.get("name") == parts[2]), None
            )
            facts.update({"kind": "input data", "namespace": parts[1], "name": f"input of {parts[2]}"})
            if consumer:
                facts["consumer"] = consumer
                facts["used_by"] = [consumer]
            facts["note"] = "name the concrete data source (topic, queue, partition, offset, table) from the evidence"
            return facts
        kind = {"service": "Service", "configmap": "ConfigMap", "secret": "Secret", **KIND_NAMES}.get(
            parts[0], parts[0]
        )
        facts.update({"kind": kind, "namespace": parts[1], "name": parts[2]})
        users = sorted(
            cid
            for cid, c in (comps.items() if isinstance(comps, dict) else [])
            if isinstance(c, dict)
            and (
                any(isinstance(s, dict) and s.get("name") == parts[2] for s in c.get("services") or [])
                if parts[0] == "service"
                else parts[2] in ((c.get("config_refs") or {}).get(parts[0] + "s") or [])
            )
        )
        if users:
            facts["used_by"] = users
        return facts
    if isinstance(comp, dict):
        facts.update({k: comp.get(k) for k in ("kind", "name", "namespace") if comp.get(k)})
        services = [sv.get("name") for sv in comp.get("services") or [] if isinstance(sv, dict) and sv.get("name")]
        if services:
            facts["services"] = services[:3]  # the Service objects in front of it, named with it in the report
    else:
        kind, _, name = origin.partition("/")
        if name:
            facts.update({"kind": kind, "name": name})
        ns = (ctx.namespaces or [None])[0]
        if ns:
            facts["namespace"] = ns
    return facts


KIND_NAMES = {
    "mutatingwebhookconfiguration": "MutatingWebhookConfiguration",
    "validatingwebhookconfiguration": "ValidatingWebhookConfiguration",
    "resourcequota": "ResourceQuota",
    "limitrange": "LimitRange",
    "clusterrole": "ClusterRole",
    "role": "Role",
    "clusterrolebinding": "ClusterRoleBinding",
    "rolebinding": "RoleBinding",
    "networkpolicy": "NetworkPolicy",
}

# Policy objects that act on workloads (as in jev_diag's report): the object is the root cause, "acting on" the
# workloads it constrains or rewrites.
CARRIER_KINDS = {
    "mutatingwebhookconfiguration",
    "validatingwebhookconfiguration",
    "resourcequota",
    "limitrange",
    "clusterrole",
    "role",
    "clusterrolebinding",
    "rolebinding",
    "networkpolicy",
}


def _object_lines(notebook: Notebook | None, origin: str) -> list[str]:
    """Every line observed about an object: the observations that targeted it, and lines elsewhere that name it."""
    if notebook is None:
        return []
    name = origin.split("/")[-1]
    rx = re.compile(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])")
    out: list[str] = []
    for o in notebook.observations:
        if o.target == origin:
            out += o.lines
        else:
            out += [ln for ln in o.lines if rx.search(ln)]
    return out


def acted_on(ctx: AgentContext, origin: str | None, notebook: Notebook | None) -> list[str]:
    """Application workloads an object origin acts on: those that use it, else those the lines observed about the
    object name (by workload name, Service name or backend-role synonym)."""
    facts = origin_facts(ctx, origin)
    if facts.get("used_by"):
        return list(facts["used_by"])
    comps = getattr(ctx.snapshot, "components", None) or {}
    at_origin = _object_lines(notebook, origin) if origin else []
    if not isinstance(comps, dict) or not at_origin:
        return []
    from clients.jev_diag import derive

    try:
        aliases = roles.name_aliases(comps)
        found = set()
        for line in at_origin:
            found |= derive._mentions(line, aliases, exclude="")
    except Exception as exc:  # noqa: BLE001 - framing is optional
        logger.debug("component mentions for the report failed: %s", exc)
        return []
    return sorted(c for c in found if c != origin)[:3]


def framing(ctx: AgentContext, origin: str | None, notebook: Notebook | None) -> tuple[str | None, list[str]]:
    """How the report names an object origin, following jev_diag's report:
    "object_acting_on"     a policy object (webhook, quota, LimitRange, RBAC, NetworkPolicy) acting on workloads;
    "targeted_component"   a shared object outside the application (cluster DNS config, ...) changed to act on
                           one workload: that workload is the root-cause component, the object is the mechanism;
    None                   anything else (a workload, an object of the application itself, data, an interaction)."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    if not origin or origin == CLUSTER_OBJECT or origin in comps:
        return None, []
    parts = origin.split("/")
    if parts[0] in ("data", "interaction"):
        return None, []
    targets = acted_on(ctx, origin, notebook)
    if not targets:
        return None, []
    if parts[0].lower() in CARRIER_KINDS:
        return "object_acting_on", targets
    namespace = parts[1] if len(parts) == 3 else None
    if namespace is None or namespace not in (ctx.namespaces or []):
        return "targeted_component", targets
    return None, []


def _workload_ref(ctx: AgentContext, cid: str) -> str:
    """'<Kind> `name` in namespace `ns`' for a component, or its id in backticks when it is unknown."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    c = comps.get(cid) if isinstance(comps, dict) else None
    if isinstance(c, dict) and c.get("name"):
        ns = f" in namespace `{c['namespace']}`" if c.get("namespace") else ""
        return f"{c.get('kind', 'workload')} `{c['name']}`{ns}"
    return f"`{cid}`"


def _comp_name(ctx: AgentContext, cid: str) -> str:
    """The component's name (the id's last part when it is unknown)."""
    comps = getattr(ctx.snapshot, "components", None) or {}
    return ((comps.get(cid) or {}) if isinstance(comps, dict) else {}).get("name") or cid.split("/")[-1]


def propagation(ctx: AgentContext, notebook: Notebook, origin: str | None) -> list[str]:
    """'A's errors name B' links from the logs tool, those touching the origin first (at most 4)."""
    edges = error_edges(notebook)
    ent = origin_entity(origin)
    edges.sort(key=lambda e: ent not in (_comp_name(ctx, e[0]), _comp_name(ctx, e[1])))
    return [f"{a}'s ongoing errors name {b}" for a, b in edges[:4]]


def link_partners(ctx: AgentContext, notebook: Notebook, origin: str | None) -> set[str]:
    """Components on the other end of an error link with the origin."""
    ent = origin_entity(origin)
    out = set()
    for a, b in error_edges(notebook):
        if _comp_name(ctx, a) == ent:
            out.add(b)
        if _comp_name(ctx, b) == ent:
            out.add(a)
    return out


MAX_ORIGIN_LINES = 10


def _origin_names(ctx: AgentContext, origin: str | None) -> tuple[set[str], set[str]]:
    """(component ids, object names) the origin stands for: a workload id; for "<kind>/<ns>/<name>" objects the
    name; for interactions both components; for input data its consumer."""
    if not origin or origin == CLUSTER_OBJECT:
        return set(), set()
    facts = origin_facts(ctx, origin)
    parts = origin.split("/")
    comps = {origin} if len(parts) == 2 else set(facts.get("components") or [])
    if facts.get("consumer"):
        comps.add(facts["consumer"])
    names = {parts[2]} if len(parts) == 3 and parts[0] not in ("interaction", "data") else set()
    return comps, names


def _routine(line: str) -> bool:
    """Lines that only report routine activity: Normal events (scheduled, pulled, started, scaled)."""
    body = line.split(": ", 1)[-1] if line.startswith("events") else line
    return body.lstrip().startswith("Normal ")


def origin_findings(ctx: AgentContext, notebook: Notebook, origin: str | None) -> list[str]:
    """Every line the investigation recorded about the origin itself, in full: the lines of abnormal observations
    whose target is the origin's component(s), and any line that names the origin object. These state the
    mechanism in Kubernetes' own words (event and condition messages, spec values) and are always reported.
    Lines from observations judged abnormal come first; routine Normal events are left out."""
    comps, names = _origin_names(ctx, origin)
    if not comps and not names:
        return []
    token = re.compile("|".join(rf"(?<![a-z0-9-]){re.escape(n)}(?![a-z0-9-])" for n in names)) if names else None
    ranked: list[tuple[int, int, str]] = []
    for o in notebook.observations:
        ends = {part.strip() for part in o.target.split("->")}
        abnormal = (o.abnormal or 0.0) >= 0.5
        for i, ln in enumerate(o.lines):
            if _routine(ln):
                continue
            if ends & comps and abnormal:
                ranked.append((0, o.step * 100 + i, f"{o.id} {o.tool} {o.target}: {ln}"))
            elif token is not None and token.search(ln):
                ranked.append((0 if abnormal else 1, o.step * 100 + i, f"{o.id} {o.tool} {o.target}: {ln}"))
    ranked.sort()
    return list(dict.fromkeys(text for _, _, text in ranked))[:MAX_ORIGIN_LINES]


def select_evidence(
    asker: Any, notebook: Notebook, inv: InvestigationResult, ctx: AgentContext
) -> tuple[list[Observation], dict[str, float], str | None]:
    """One decision request; returns (selected observations, P(support) per id, error)."""
    obs = [o for o in inv.observations if o.lines]
    if not obs:
        return [], {}, None
    # abnormal ones first, then most recent
    ordered = sorted(obs, key=lambda o: (-(o.abnormal if o.abnormal is not None else 0.5), -o.step))
    asked = sorted(ordered[:MAX_EVIDENCE_QUESTIONS], key=lambda o: o.step)
    category = inv.fault_category or "other"
    conclusion = {
        "origin": origin_facts(ctx, inv.origin),
        "fault_category": category,
        "mechanism": CATEGORY_GLOSS.get(category, category),
    }
    questions = {
        f"supports_{o.id}": Noul(
            instructions=f"Does observation {o.id} ({o.tool} on {o.target}) in `observations` support the "
            "conclusion in `conclusion`: that this origin is the source of the fault, through this mechanism?",
            criteria={
                "true": "It shows the origin's defect, its mechanism, or its direct effect on other components.",
                "false": "It is unrelated, healthy, or points to a different origin.",
            },
        )
        for o in asked
    }
    state = {**notebook.state(20000), "conclusion": conclusion}
    try:
        response = asker.ask("evidence", state, questions)
    except Exception as exc:  # noqa: BLE001 - fall back to the abnormal observations
        fallback = [o for o in asked if (o.abnormal or 0) >= 0.5][:MAX_EVIDENCE_LINES] or asked[-3:]
        return fallback, {}, f"{type(exc).__name__}: {exc}"
    nouls = getattr(response, "nouls", None) or {}
    support = {o.id: float(nouls[f"supports_{o.id}"].noul) for o in asked if f"supports_{o.id}" in nouls}
    selected = [o for o in asked if support.get(o.id, 0.0) >= SUPPORT_P]
    if not selected:
        selected = sorted(asked, key=lambda o: -support.get(o.id, 0.0))[:3]
        selected.sort(key=lambda o: o.step)
    selected = sorted(selected, key=lambda o: -support.get(o.id, 0.0))[:MAX_EVIDENCE_LINES]
    # abnormal findings on the origin's direct link partners describe the mechanism even when they do not
    # "support the origin" on their own (e.g. how a caller uses it)
    partners = link_partners(ctx, notebook, inv.origin)
    extra = [
        o
        for o in asked
        if o not in selected and (o.abnormal or 0.0) >= 0.5 and o.target.split("->")[0].strip() in partners
    ]
    selected = (selected + extra)[: MAX_EVIDENCE_LINES + 2]
    selected.sort(key=lambda o: o.step)
    return selected, support, None


def affected_neighbors(
    ctx: AgentContext,
    origin: str | None,
    scores: dict[str, float],
    overview_mod: Any,
    notebook: Notebook | None = None,
) -> list[str]:
    """Components the failure reaches: those whose ongoing errors name the origin (observed propagation); for an
    object, the workloads that use it; else abnormal clients of the origin from the dependency map."""
    if origin and origin.count("/") == 2 and not origin.startswith(("interaction/", "data/")):
        return list(origin_facts(ctx, origin).get("used_by") or [])[:8]
    if not origin or origin == CLUSTER_OBJECT:
        return []
    ent = origin_entity(origin)
    if notebook is not None:
        callers = [a for a, b in error_edges(notebook) if _comp_name(ctx, b) == ent and _comp_name(ctx, a) != ent]
        if callers:
            return list(dict.fromkeys(callers))[:8]
    if overview_mod is None:
        return []
    view = ((ctx.overview or {}).get("components") or {}).get(origin) or {}
    clients = [c for c in view.get("clients") or [] if scores.get(c, 0.0) > 0]
    return clients[:8]


def template_text(
    ctx: AgentContext,
    inv: InvestigationResult,
    evidence: list[Observation],
    affected: list[str],
    links: list[str] | None = None,
    at_origin: list[str] | None = None,
    notebook: Notebook | None = None,
) -> str:
    """The diagnosis written from a template (used without an LLM, or when the LLM fails)."""
    facts = origin_facts(ctx, inv.origin)
    frame, targets = framing(ctx, inv.origin, notebook)
    lines: list[str] = []
    if inv.origin == CLUSTER_OBJECT:
        lines.append(
            "Root cause: a cluster-level object outside the application workloads "
            f"(namespaces {', '.join(ctx.namespaces or [])}); see the evidence below."
        )
    elif frame == "object_acting_on":
        ns = f" in namespace `{facts['namespace']}`" if facts.get("namespace") else ""
        on = ", ".join(_workload_ref(ctx, c) for c in targets)
        lines.append(f"Root cause object: {facts.get('kind', 'object')} `{facts.get('name')}`{ns}, acting on {on}.")
    elif frame == "targeted_component":
        ns = f" in namespace `{facts['namespace']}`" if facts.get("namespace") else ""
        lines.append(f"Root cause component: {', '.join(_workload_ref(ctx, c) for c in targets)}.")
        lines.append(
            f"Fault object: {facts.get('kind', 'object')} `{facts.get('name')}`{ns}, changed so that it acts on this "
            "component (see the observations below)."
        )
    elif facts.get("name"):
        ns = f" in namespace `{facts['namespace']}`" if facts.get("namespace") else ""
        label = "Root cause object" if inv.origin and inv.origin.count("/") == 2 else "Root cause component"
        served = (
            " (with its Service " + ", ".join(f"`{n}`" for n in facts["services"]) + ")"
            if facts.get("services")
            else ""
        )
        lines.append(f"{label}: {facts.get('kind', 'workload')} `{facts['name']}`{served}{ns}.")
    elif inv.origin:
        lines.append(f"Root cause component: `{inv.origin}`.")
    else:
        lines.append("Root cause: not determined from the collected evidence.")
    cat = inv.fault_category
    if cat:
        lines.append(f"Fault category: {cat.replace('_', ' ')} ({CATEGORY_GLOSS.get(cat, cat)}).")
    reviewed = (ctx.extra or {}).get("origin_review") or {}
    if reviewed.get("mechanism"):
        lines.append(f"Mechanism: {reviewed['mechanism']}")
    if at_origin:
        lines.append("Observed at the origin (verbatim):")
        lines += [f"- {ln}" for ln in at_origin]
    shown = {ln.split(":", 1)[0] for ln in at_origin or []}
    rest = [o for o in evidence if not any(x.startswith(f"{o.id} ") for x in shown)]
    if rest:
        lines.append("Other supporting evidence:")
        lines += [f"- {obs_line(o)}" for o in rest]
    if links:
        lines.append("Propagation: " + "; ".join(links) + ".")
    if affected:
        lines.append(f"Affected components: {', '.join(affected)}.")
    return "\n".join(lines)


def _framed_origin(ctx: AgentContext, origin: str | None, notebook: Notebook | None) -> dict:
    """The origin's facts, plus its framing and the objects it acts on when it is framed."""
    facts = dict(origin_facts(ctx, origin))
    frame, targets = framing(ctx, origin, notebook)
    if frame:
        facts["framing"] = frame
        facts["acts_on"] = targets
    return facts


def llm_text(
    llm: Any,
    ctx: AgentContext,
    inv: InvestigationResult,
    evidence: list[Observation],
    affected: list[str],
    links: list[str] | None = None,
    at_origin: list[str] | None = None,
    notebook: Notebook | None = None,
) -> str:
    """The diagnosis written by the LLM from the selected evidence only."""
    cat = inv.fault_category or "other"
    facts = {
        "application": ctx.app_info.get("app_name") or ctx.app_info.get("name"),
        "namespaces": ctx.namespaces,
        "origin": _framed_origin(ctx, inv.origin, notebook),
        "fault_category": cat,
        "fault_category_meaning": CATEGORY_GLOSS.get(cat, cat),
        "evidence": [obs_line(o, max_lines=4) for o in evidence],
        "affected_components": affected,
        "propagation": links or [],
        "observed_at_origin": at_origin or [],
        "why_this_origin": (ctx.extra or {}).get("origin_review") or {},
    }
    user = "Facts:\n" + json.dumps(facts, ensure_ascii=False, indent=1, default=str) + "\n\nWrite the diagnosis."
    resp = llm.complete(WRITEUP_SYSTEM, user, max_output_tokens=2000)
    text = (resp.text or "").strip()
    if len(text.split()) < MIN_WORDS:
        raise ValueError(f"write-up too short ({len(text.split())} words)")
    return text


def build_report(
    asker: Any,
    notebook: Notebook,
    inv: InvestigationResult,
    ctx: AgentContext,
    *,
    llm: Any = None,
    max_llm_calls: int = 2,
    scores: dict[str, float] | None = None,
    overview_mod: Any = None,
    select: bool = True,
) -> Report:
    """Select the supporting evidence and write the diagnosis (LLM first, template as fallback)."""
    if select:
        evidence, support, ev_error = select_evidence(asker, notebook, inv, ctx)
    else:
        evidence, support, ev_error = (
            [o for o in inv.observations if (o.abnormal or 0) >= 0.5][:MAX_EVIDENCE_LINES],
            {},
            None,
        )
    affected = affected_neighbors(ctx, inv.origin, scores or notebook.scores, overview_mod, notebook)
    links = propagation(ctx, notebook, inv.origin)
    at_origin = origin_findings(ctx, notebook, inv.origin)
    lines = at_origin + [obs_line(o) for o in evidence]
    if llm is not None and getattr(llm, "calls", 0) < max_llm_calls:
        try:
            text = llm_text(llm, ctx, inv, evidence, affected, links, at_origin, notebook)
            return Report(text, "llm", lines, support, affected, evidence_error=ev_error)
        except Exception as exc:  # noqa: BLE001 - the template is always available
            logger.warning("LLM write-up failed, using template: %s", exc)
            return Report(
                template_text(ctx, inv, evidence, affected, links, at_origin, notebook),
                "template",
                lines,
                support,
                affected,
                llm_error=f"{type(exc).__name__}: {exc}",
                evidence_error=ev_error,
            )
    return Report(
        template_text(ctx, inv, evidence, affected, links, at_origin, notebook),
        "template",
        lines,
        support,
        affected,
        evidence_error=ev_error,
    )


def results_payload(inv: InvestigationResult, report: Report, asker: Any, llm: Any, *, submission_text: str) -> dict:
    """The jev_agent-specific results fields (merged into the results json)."""
    llm_calls = getattr(llm, "calls", 0) if llm is not None else 0
    llm_in = getattr(llm, "input_tokens", 0) if llm is not None else 0
    llm_out = getattr(llm, "output_tokens", 0) if llm is not None else 0
    return {
        **inv.summary(),
        "decision_calls": asker.calls,
        "input_tokens": asker.input_tokens,
        "output_tokens": asker.output_tokens,
        "llm_model": getattr(llm, "model", None),
        "llm_calls": llm_calls,
        "llm_tokens": llm_in + llm_out,
        "llm_input_tokens": llm_in,
        "llm_output_tokens": llm_out,
        "report_source": report.source,
        "report_llm_error": report.llm_error,
        "evidence": report.evidence,
        "evidence_support": report.support,
        "affected": report.affected,
        "submission_text": submission_text,
    }
