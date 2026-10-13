"""Helpers shared by the tools: component lookup, kubectl reads, pod state and time phrasing.

The tool modules call the kubectl reads (_get_json, _items, _ns_pods, _current_pod_names) as
`_common.<name>(...)`, so a test that stubs one patches it here, once, for every tool.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from clients.jev_agent import metrics
from clients.jev_agent.evidence import write_note
from clients.jev_agent.overview import snapshot_parts
from clients.jev_agent.types import AgentContext
from clients.jev_diag import changes as change_log
from clients.jev_diag import collector, investigate
from clients.jev_diag.timeutil import now, parse_time, seconds_between

logger = logging.getLogger("all.jev_agent.tools")

_CREDENTIAL_RE = re.compile(r"(?i)(pass(word)?|pwd|auth|token|secret|credential|api[_-]?key|private[_-]?key)")

# Event and condition messages are where Kubernetes states the reason (quota, volume binding, admission);
# their decisive clause often comes after the first 100 characters.
MESSAGE_CHARS = 320
REVISION_ITEMS = 6  # changed fields listed per pod-template revision
# Conditions that only restate that replicas are missing, without a cause.
CAUSELESS_REASONS = {"MinimumReplicasUnavailable", "ProgressDeadlineExceeded"}


def _components(ctx: AgentContext) -> dict[str, dict]:
    """The snapshot's components by id."""
    return snapshot_parts(ctx.snapshot)[1]


def _namespaces(ctx: AgentContext) -> list[str]:
    """The application's namespaces (from the context, else from the overview)."""
    return list(ctx.namespaces or (ctx.overview.get("app") or {}).get("namespaces") or [])


def _comp(ctx: AgentContext, cid: str) -> dict:
    """The snapshot component with id `cid`; KeyError when there is none."""
    comp = _components(ctx).get(cid)
    if comp is None:
        raise KeyError(f"unknown component {cid}")
    return comp


def _snapshot_obj(ctx: AgentContext) -> Any:
    """An object with .components/.cluster (what jev_diag.probes expects), for a ClusterSnapshot or a dict."""
    if hasattr(ctx.snapshot, "components"):
        return ctx.snapshot
    _, comps, cluster, collected_at = snapshot_parts(ctx.snapshot)
    return SimpleNamespace(components=comps, cluster=cluster, collected_at=collected_at)


def _get_json(args: list[str]) -> dict:
    """`kubectl <args>` output parsed as JSON (raises when kubectl fails)."""
    return json.loads(collector.run_kubectl(args))


def _items(resource: str, namespace: str | None, *, managed: bool = False) -> list[dict]:
    """`kubectl get <resource>` items with the argument order collector.kubectl_items uses (so recorded bundles
    replay), but raising on failure instead of returning []."""
    args = ["get", resource, "-o", "json"] + (["--show-managed-fields"] if managed else [])
    args += ["-n", namespace] if namespace else ["-A"]
    return _get_json(args).get("items", [])


def _onset(ctx: AgentContext) -> datetime | None:
    """The overview's symptom-onset estimate, or None."""
    return parse_time((ctx.overview.get("cluster") or {}).get("symptom_onset"))


def _when(ctx: AgentContext, ts: str | None) -> str:
    """'<t> (Ns before symptom onset)' / '(Ns after ...)' / '(Ns ago)'."""
    moment = parse_time(ts)
    if moment is None:
        return "at unknown time"
    onset = _onset(ctx)
    if onset is not None:
        delta = seconds_between(onset, moment)
        if delta is not None:
            # the onset estimate is coarse: within ONSET_TOLERANCE_SECONDS after it still counts as before (changes.py)
            if -change_log.ONSET_TOLERANCE_SECONDS <= delta < 0:
                return f"at symptom onset ({abs(delta)}s after the onset estimate)"
            return f"{abs(delta)}s {'before' if delta >= 0 else 'after'} symptom onset"
    ago = seconds_between(now(), moment)
    return f"{ago}s ago" if ago is not None else str(ts)


def _written(ctx: AgentContext, created: str | None, last_write: str | None) -> str:
    """'created <when>; not written since' or 'created <when>; written again <when> (Ns after creation)'
    (a write within changes.MIN_CHANGE_SECONDS of creation is part of creating it), noting a write that cannot have
    started the symptoms (evidence.write_note)."""
    since = seconds_between(parse_time(last_write), parse_time(created)) if created and last_write else None
    text = f"created {_when(ctx, created or last_write)}"
    if since is not None and since >= change_log.MIN_CHANGE_SECONDS:
        text += f"; written again {_when(ctx, last_write)} ({since}s after creation)"
        return text + write_note(ctx, last_write)
    return text + "; not written since"


def _ago(ts: str | None) -> str:
    """'Ns ago' for a timestamp, or '?' when it cannot be read."""
    delta = seconds_between(now(), parse_time(ts))
    return f"{delta}s ago" if delta is not None else "?"


def _join(parts: list[str], sep: str = "; ") -> str:
    """The non-empty parts joined with `sep`."""
    return sep.join(p for p in parts if p)


def _pod_matches(comp: dict, pod_name: str) -> bool:
    """Whether a pod name follows the component's pod naming scheme."""
    return bool(re.fullmatch(metrics.pod_name_regex(comp), pod_name or ""))


def _owner_of(components: dict[str, dict], pod: dict) -> str | None:
    """Component id of a raw pod: owner references first, then the controller naming scheme."""
    meta = pod.get("metadata") or {}
    ns, name = meta.get("namespace"), meta.get("name") or ""
    for ref in meta.get("ownerReferences") or []:
        kind, owner = ref.get("kind"), ref.get("name") or ""
        if kind == "ReplicaSet":
            kind, owner = "Deployment", owner.rsplit("-", 1)[0]
        cid = collector.component_id(kind or "", owner)
        if cid in components and components[cid].get("namespace") == ns:
            return cid
    for cid, comp in components.items():
        if comp.get("namespace") == ns and _pod_matches(comp, name):
            return cid
    return None


def _ns_pods(ns: str) -> list[dict]:
    """The raw pods of a namespace as they are now."""
    return _items("pods", ns)


def _current_pod_names(ctx: AgentContext, comp: dict) -> list[str]:
    """The component's pods as they are now (falls back to the snapshot's pod list)."""
    try:
        comps = _components(ctx)
        cid = collector.component_id(comp["kind"], comp["name"])
        names = [p["metadata"]["name"] for p in _ns_pods(comp["namespace"]) if _owner_of(comps, p) == cid]
        if names:
            return names
    except Exception as exc:  # noqa: BLE001
        logger.info("pod list for %s failed: %s", comp.get("name"), exc)
    return [p.get("name") for p in comp.get("pods") or [] if p.get("name")]


def _running_pod(comp: dict) -> tuple[str | None, str | None]:
    """(pod, first app container) of a Running pod, ready pods first; (None, None) when there is none."""
    pods = [p for p in comp.get("pods") or [] if p.get("phase") == "Running"]
    pods.sort(key=lambda p: p.get("ready", "0/0").split("/")[0] == "0")
    containers = [c for c in comp.get("containers") or [] if not c.get("init")]
    if not pods or not containers:
        return None, None
    return pods[0].get("name"), containers[0].get("name")


def _secretish(name: str | None) -> bool:
    """Whether a name looks like a credential (password, token, key, ...)."""
    return bool(_CREDENTIAL_RE.search(name or ""))


def _ends(text: str, n: int = MESSAGE_CHARS) -> str:
    """`text` cut to about n characters keeping its start and its end: error chains put the decisive cause last."""
    text = " ".join(str(text).split())
    if len(text) <= n:
        return text
    head = n // 3
    return text[:head] + " … " + text[-(n - head - 3) :]


def _warmup_crash(pod: dict, cs: dict) -> tuple[int, int] | None:
    """(seconds after pod start the last crash ended, seconds the current run has lasted) for a container that
    crashed only during warm-up and has been ready and running since (investigate._settled_restart, the rule the
    overview applies to restarts). Else None."""
    if not investigate._settled_restart(pod, cs):
        return None
    term = ((cs.get("last_state") or {}).get("terminated")) or {}
    started = parse_time(pod.get("started"))
    finished = parse_time(term.get("finishedAt"))
    running = parse_time(((cs.get("state") or {}).get("running") or {}).get("startedAt"))
    return int((finished - started).total_seconds()), int((now() - running).total_seconds())
