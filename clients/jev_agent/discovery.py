"""Environment discovery and LLM tool crafting.

Before the investigation (`explore`): the program takes a generic inventory of the environment (API resources and
CRDs the cluster serves, metric names Prometheus holds for the application's namespaces, the components with
their images and backend roles, and the existing tool menu); the LLM reads it and writes up to MAX_EXPLORE_TOOLS
crafted tools (see crafted.py) for what the menu does not cover; each one is smoke-tested on one applicable
component and kept only if it returns data.

During the investigation (`craft_now`): when the decision model picks the CRAFT option, the LLM sees the notebook
and writes one crafted tool plus the component to run it on; it is validated, registered and run at once.

The LLM proposes observations, never conclusions: prompts ask for tools that show state, and validation (in
crafted.py) rejects anything that is not read-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clients.jev_agent import crafted, metrics
from clients.jev_agent.roles import backend_role
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import collector

logger = logging.getLogger("all.jev_agent.discovery")

MAX_EXPLORE_TOOLS = 6
CACHE_VERSION = 1
CRAFT_RETRIES = 1  # a rejected in-task tool is rewritten once, with the rejection reason
MAX_METRIC_NAMES = 120
MAX_CRDS = 40
INFRA_METRIC = metrics._INFRA_METRIC_RE

PRIMITIVES_DOC = (
    """Each tool is JSON: {"name": "snake_case", "reveals": "<one sentence: what it shows>",
 "applies_to": {"role": "<backend role or null>", "image_contains": "<image substring or null>"},
 "steps": [1 to 3 steps]}. A tool runs on one component; placeholders {namespace} {name} {pod} {pod_regex}
 {service} are filled for that component. Step kinds (all read-only):
 - {"op": "k8s_get", "resource": "<api resource from api_resources>", "name": "<optional name/template>",
    "namespaced": true, "fields": ["<dot path>", ...]}   (Secrets are not allowed)
 - {"op": "promql", "query": "<instant PromQL using names from metric_names>"}   (summarised over 15 minutes)
 - {"op": "logs_grep", "pattern": "<regex>", "previous": false}
 - {"op": "exec", "command": ["<binary>", "<args>"]}   binary one of: """
    + ", ".join(sorted(crafted.READONLY_BINARIES))
    + """;
    no write/admin subcommands, no shell, no credentials, no env printing; curl/wget only GET one http(s) URL
    to stdout (curl -sS -i -L -m N -H 'Accept: ...'; wget -q -O- -T N)
 - {"op": "http_get", "port": <service port>, "path": "/<path>", "grep": "<optional regex>"}  (GET to the
    component's Service, through the API server proxy or from inside a pod of its namespace)"""
)

EXPLORE_SYSTEM = (
    "You prepare read-only observation tools for investigating incidents in the Kubernetes application described "
    "in the JSON. An investigator already has the tools in `existing_tools`. Write tools for state that those "
    "tools cannot show in this environment: backends without a health or status command, objects of custom "
    "resources, application metrics by their real names, status endpoints. Tools must observe state, not judge "
    "it; do not guess what might be broken. Prefer tools that apply to several components. "
    + PRIMITIVES_DOC
    + f' Answer with a JSON object only: {{"tools": [<at most {MAX_EXPLORE_TOOLS} tools>]}}.'
)

CRAFT_SYSTEM = (
    "You help an incident investigation that has run out of suitable tools. Read the notebook (application, "
    "overview, observations O1..On) and write ONE read-only tool that would show the state the evidence points "
    "to but no existing tool covers, and the component to run it on. It must observe, not conclude. "
    + PRIMITIVES_DOC
    + " If `previous_answer` is present it was rejected for `previous_answer_rejected_because`: fix that. "
    + ' Answer with a JSON object only: {"tool": <tool>, "component": "<one id from `components`>", '
    '"reason": "<one sentence>"}.'
)


@dataclass
class ExploreResult:
    """What the explore phase produced: the inventory it saw, the tools kept and dropped, and the LLM usage."""

    inventory: dict = field(default_factory=dict)
    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    error: str | None = None
    usage: dict = field(default_factory=dict)
    raw_text: str = ""
    # (tool, target, result) of each kept tool's smoke test: real read-only observations of the environment
    trials: list[tuple[str, str, ToolResult]] = field(default_factory=list)
    cache: str | None = None  # None (no cache) | "hit" | "miss+saved" | "stale+saved" | ...


# --------------------------------------------------------------------------- inventory


def api_resources() -> set[str]:
    """Names (plural, with and without group) of resources the cluster serves with `get`."""
    out: set[str] = set()
    for scope in ("true", "false"):
        try:
            text = collector.run_kubectl(["api-resources", "--verbs=get", f"--namespaced={scope}", "-o", "name"])
        except Exception as exc:  # noqa: BLE001 - an inventory gap, not a failure
            logger.info("api-resources failed: %s", exc)
            continue
        for line in text.split():
            out.add(line.strip().lower())
            out.add(line.strip().lower().split(".")[0])
    return out


def inventory(ctx: AgentContext, registry: dict) -> dict:
    """A compact description of the environment for the LLM: API resources, CRDs, components with images and roles,
    and the built-in tools."""
    from clients.jev_agent.tools._common import _components

    resources = api_resources()
    ctx.extra["api_resources"] = resources
    crds = []
    try:
        for item in json.loads(collector.run_kubectl(["get", "crd", "-o", "json"])).get("items", [])[:MAX_CRDS]:
            spec = item.get("spec") or {}
            crds.append(f"{(spec.get('names') or {}).get('plural')}.{spec.get('group')} ({spec.get('scope')})")
    except Exception as exc:  # noqa: BLE001
        logger.info("crd list failed: %s", exc)
    names: list[str] = []
    try:
        call = ctx.extra.get("prom_call") or metrics.prom_call()
        ns = "|".join(ctx.namespaces)
        q = f'count by (__name__)({{namespace=~"{ns}",__name__!~"{INFRA_METRIC}"}})'
        names = sorted(
            {s["labels"].get("__name__") for s in metrics.parse_prom(call(q)) if s["labels"].get("__name__")}
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("metric inventory failed: %s", exc)
    comps = []
    for cid, comp in _components(ctx).items():
        comps.append(
            {
                "id": cid,
                "images": sorted({str(c.get("image")) for c in comp.get("containers") or [] if c.get("image")})[:3],
                "role": backend_role(comp),
                "services": [
                    {"name": s.get("name"), "ports": s.get("ports")}
                    for s in comp.get("services") or []
                    if isinstance(s, dict)
                ][:2],
            }
        )
    return {
        "application": ctx.app_info.get("app_name"),
        "namespaces": ctx.namespaces,
        "api_resources": sorted(r for r in resources if "." not in r)[:150],
        "custom_resources": crds,
        "metric_names": names[:MAX_METRIC_NAMES],
        "components": comps,
        "existing_tools": {name: str(spec.reveals) for name, spec in registry.items()},
    }


# --------------------------------------------------------------------------- LLM calls


RAW_TEXT_CHARS = 4000


def _ask(llm: Any, system: str, payload: dict) -> tuple[dict | None, dict, str | None, str]:
    """(parsed answer, usage, error, raw text) for one LLM call."""
    try:
        resp = llm.complete_json(system, json.dumps(payload, ensure_ascii=False, default=str), max_output_tokens=4000)
    except Exception as exc:  # noqa: BLE001 - crafting is optional
        logger.warning("LLM call for tool crafting failed: %s: %s", type(exc).__name__, exc)
        return None, {}, f"{type(exc).__name__}: {exc}", ""
    text = getattr(resp, "text", "") or ""
    data = getattr(resp, "data", None)
    if data is None:
        from clients.jev_agent.llm import parse_json_object

        data = parse_json_object(text)
    error = None if isinstance(data, dict) else "answer is not JSON"
    return data, dict(getattr(resp, "usage", {}) or {}), error, text[:RAW_TEXT_CHARS]


def _smoke_target(ctx: AgentContext, tool: crafted.Crafted, scores: dict[str, float]) -> str | None:
    """An applicable component to try the tool on: the healthiest (lowest score) one, so a smoke test reads a
    normal baseline and does not touch the components the investigation will focus on more than needed."""
    from clients.jev_agent.tools._common import _components

    cands = [cid for cid in _components(ctx) if crafted.applies(ctx, tool, cid)]
    cands.sort(key=lambda c: (scores.get(c, 0.0), c))
    return cands[0] if cands else None


def _adopt(ctx: AgentContext, registry: dict, scores: dict[str, float], specs: list, result: ExploreResult) -> None:
    """Validate, smoke-test and register tool specs (from the LLM or the cache) into `result`."""
    for spec in specs[:MAX_EXPLORE_TOOLS]:
        tool, why = crafted.validate(spec, ctx.extra.get("api_resources"), set(registry))
        if tool is None:
            result.dropped.append({"spec": spec, "reason": why})
            continue
        target = _smoke_target(ctx, tool, scores)
        if target is None:
            result.dropped.append({"spec": spec, "reason": "no component it applies to"})
            continue
        trial = crafted.run(ctx, tool, target)
        if not crafted.useful(trial):
            result.dropped.append({"spec": spec, "reason": "smoke test returned nothing", "lines": trial.lines[:3]})
            continue
        crafted.register(registry, tool)
        result.kept.append(
            {"tool": tool.tool_name, "spec": tool.to_dict(), "smoke_target": target, "lines": trial.lines[:3]}
        )
        result.trials.append((tool.tool_name, target, trial))


@dataclass
class Proposal:
    """What exploring yields before any tool is validated or tried: the inventory and the tool specs (from the
    cache or one LLM call). It needs no fresh cluster state, so it can be prepared during the start delay."""

    inventory: dict
    resources: set[str]
    key: str
    specs: list = field(default_factory=list)
    cache: str | None = None  # "hit" when the specs came from the cache
    error: str | None = None
    usage: dict = field(default_factory=dict)
    raw_text: str = ""


def propose(llm: Any, ctx: AgentContext, registry: dict, cache_dir: str | None = None) -> Proposal:
    """Ask the LLM for environment-specific tools (or take them from the cache); nothing is validated or run yet."""
    inv = inventory(ctx, registry)
    prop = Proposal(inventory=inv, resources=set(ctx.extra.get("api_resources") or set()), key=cache_key(inv))
    cached = load_cached(cache_dir, prop.key)
    if cached:
        prop.specs, prop.cache = cached, "hit"
        return prop
    data, prop.usage, prop.error, prop.raw_text = _ask(llm, EXPLORE_SYSTEM, inv)
    prop.specs = (data.get("tools") or []) if isinstance(data, dict) else []
    return prop


def adopt(
    llm: Any, ctx: AgentContext, registry: dict, scores: dict[str, float], prop: Proposal, cache_dir: str | None = None
) -> ExploreResult:
    """Validate and smoke-test the proposed tools on the current cluster state and register those that work. A
    cached proposal of which nothing works any more is replaced by exploring again."""
    ctx.extra["api_resources"] = prop.resources
    result = ExploreResult(
        inventory=prop.inventory, error=prop.error, usage=prop.usage, raw_text=prop.raw_text, cache=prop.cache
    )
    _adopt(ctx, registry, scores, prop.specs, result)
    if prop.cache == "hit":
        if result.kept:
            return result
        result.cache = "stale"
        result.dropped.clear()
        data, result.usage, result.error, result.raw_text = _ask(llm, EXPLORE_SYSTEM, prop.inventory)
        if isinstance(data, dict):
            _adopt(ctx, registry, scores, data.get("tools") or [], result)
    if result.kept and cache_dir:
        save_cached(cache_dir, prop.key, [k["spec"] for k in result.kept], prop.inventory)
        result.cache = (result.cache or "miss") + "+saved"
    return result


def explore(
    llm: Any, ctx: AgentContext, registry: dict, scores: dict[str, float], cache_dir: str | None = None
) -> ExploreResult:
    """Tools for this environment before the investigation: from the cache when an earlier run explored the same
    environment, else from one LLM call. Either way each tool must pass validation and a smoke test now."""
    return adopt(llm, ctx, registry, scores, propose(llm, ctx, registry, cache_dir), cache_dir)


# --------------------------------------------------------------------------- explore cache
#
# Exploring costs about a minute of LLM time, and its answer depends only on the environment (what the cluster
# serves, which images run, which tools exist), not on the incident: tools observe, they do not look for a fault.
# The kept tools are stored per environment and re-validated and smoke-tested on every reuse.


def cache_key(inv: dict) -> str:
    """Environment identity: application, namespaces, the distinct images and roles, custom resources, the fixed
    tool menu and the explore prompt. Pod names, metric values and the incident do not enter it."""
    basis = {
        "version": CACHE_VERSION,
        "prompt": hashlib.sha256(EXPLORE_SYSTEM.encode()).hexdigest()[:16],
        "application": inv.get("application"),
        "namespaces": sorted(inv.get("namespaces") or []),
        "images": sorted(
            {(img, c.get("role") or "") for c in inv.get("components") or [] for img in c.get("images") or []}
        ),
        "custom_resources": sorted(inv.get("custom_resources") or []),
        "tools": sorted(k for k in inv.get("existing_tools") or {} if not k.startswith(crafted.PREFIX)),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _cache_path(cache_dir: str, key: str) -> Path:
    """Where the explore result of an environment key is cached."""
    return Path(cache_dir) / f"explore_{key}.json"


def load_cached(cache_dir: str | None, key: str) -> list[dict] | None:
    """Tool specs cached under `key`, or None when there is no cache, no entry or the entry is unreadable."""
    if not cache_dir:
        return None
    try:
        data = json.loads(_cache_path(cache_dir, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tools = data.get("tools") if isinstance(data, dict) else None
    return tools if isinstance(tools, list) and tools else None


def save_cached(cache_dir: str, key: str, specs: list[dict], inv: dict) -> None:
    """Atomic write (runs on several clusters may share the directory); failures only cost the next run time."""
    try:
        path = _cache_path(cache_dir, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "key": key,
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "application": inv.get("application"),
                    "namespaces": inv.get("namespaces"),
                    "tools": specs,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.info("explore cache not written: %s", exc)


@dataclass
class CraftResult:
    """One in-task tool written for a lead: the tool, the target it ran on, its result, and the LLM usage."""

    tool: str | None = None
    target: str | None = None
    reason: str = ""
    result: ToolResult | None = None
    error: str | None = None
    spec: dict | None = None
    usage: dict = field(default_factory=dict)
    raw_text: str = ""


def craft_now(llm: Any, ctx: AgentContext, registry: dict, notebook_state: dict) -> CraftResult:
    """One LLM call during the investigation (plus one retry that sees why the first answer was rejected):
    write, validate, register and run one tool."""
    from clients.jev_agent.tools._common import _components

    out = CraftResult()
    comps = _components(ctx)
    payload: dict[str, Any] = {
        "notebook": notebook_state,
        "existing_tools": {n: str(s.reveals) for n, s in registry.items()},
        "api_resources": sorted(r for r in (ctx.extra.get("api_resources") or set()) if "." not in r)[:150],
        "components": sorted(comps),
    }
    for _ in range(1 + CRAFT_RETRIES):
        data, usage, out.error, out.raw_text = _ask(llm, CRAFT_SYSTEM, payload)
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                out.usage[k] = out.usage.get(k, 0) + v
        if data is None:
            return out
        out.spec = data.get("tool")
        tool, why = crafted.validate(out.spec, ctx.extra.get("api_resources"), set(registry))
        target = str(data.get("component") or "")
        if tool is not None and (target not in comps or not crafted.applies(ctx, tool, target)):
            why = f"component {target!r} is not one of `components` or the tool's applies_to excludes it"
        if tool is None or why:
            out.error = f"rejected: {why}"
            payload["previous_answer"] = data
            payload["previous_answer_rejected_because"] = why
            continue
        out.error = None
        tool.source = "in_task"
        tool.only_target = target
        crafted.register(registry, tool)
        out.tool, out.target, out.reason = tool.tool_name, target, str(data.get("reason") or "")
        out.result = crafted.run(ctx, tool, target)
        return out
    return out
