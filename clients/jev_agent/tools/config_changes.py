"""config_and_changes: spec writers, pod template revisions, and the ConfigMaps and Secrets a workload reads."""

from __future__ import annotations

import logging

from clients.jev_agent.evidence import write_note
from clients.jev_agent.tools import _common
from clients.jev_agent.tools._common import REVISION_ITEMS, _comp, _when, _written
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import changes as change_log
from clients.jev_diag import collector, investigate
from clients.jev_diag.timeutil import parse_time, seconds_between

logger = logging.getLogger("all.jev_agent.tools")


def _revision_lines(ctx: AgentContext, comp: dict, meta: dict, errors: list[str]) -> tuple[list[str], list[dict]]:
    """(lines, revision history) for a workload's last two pod-template revisions: what each changed and when, or
    that it rolled out with the workload itself. List read failures go to `errors`."""
    ns, kind, name = comp["namespace"], comp["kind"], comp["name"]
    lines: list[str] = []
    if kind == "Deployment":
        revs = change_log.revision_templates(
            collector.kubectl_items("replicasets", ns, errors, managed_fields=False), []
        )
    elif kind in ("StatefulSet", "DaemonSet"):
        revs = change_log.revision_templates(
            [], collector.kubectl_items("controllerrevisions", ns, errors, managed_fields=False)
        )
    else:
        revs = {}
    history = revs.get((ns, kind, name)) or []
    for old, new in list(zip(history, history[1:], strict=False))[-2:]:
        diff = change_log.template_diff(old["template"], new["template"])
        if not diff:
            continue
        since_creation = seconds_between(parse_time(new.get("created")), parse_time(meta.get("creationTimestamp")))
        if since_creation is not None and since_creation < change_log.MIN_CHANGE_SECONDS:
            lines.append(
                f"revision {new['revision']} rolled out with the deployment itself ({since_creation}s after creation; "
                "not a later change): " + "; ".join(diff[:2])
            )
        else:
            # one change per line, the container's own settings first; the pod-template-hash label changes with
            # every revision and says nothing
            items = [d for d in diff if not d.startswith("pod label pod-template-hash")]
            items.sort(key=lambda d: d.startswith(("template annotation", "pod label")))
            note = write_note(ctx, new.get("created"))
            if note:  # history: listed on one line, not as changes
                lines.append(
                    f"revision {new['revision']} created {_when(ctx, new.get('created'))}{note}: "
                    + "; ".join(items[:REVISION_ITEMS])
                )
                continue
            lines.append(f"revision {new['revision']} created {_when(ctx, new.get('created'))}: {len(items)} change(s)")
            lines += [f"revision {new['revision']} change: {d}" for d in items[:REVISION_ITEMS]]
    if len(history) < 2:
        lines.append(f"pod template revisions: {len(history)} (no earlier template to compare)")
    return lines, history


def _config_ref_lines(ctx: AgentContext, comp: dict) -> list[str]:
    """The data and write times of the ConfigMaps a workload reads, and the key names (never values) of up to three
    of its Secrets."""
    ns = comp["namespace"]
    lines: list[str] = []
    refs = comp.get("config_refs") or {}
    cms = [n for n in refs.get("configmaps") or [] if n]
    if cms:
        for cm_name, detail in investigate.configmap_detail(cms, ns).items():
            if detail.get("error"):
                lines.append(f"ConfigMap {cm_name}: {detail['error']}")
                continue
            data = "; ".join(f"{k}={v}" for k, v in (detail.get("data") or {}).items())
            lines.append(
                f"ConfigMap {cm_name} ({_written(ctx, detail.get('created'), detail.get('last_update'))}): "
                f"{data or 'no data'}"
            )
    for secret in [n for n in refs.get("secrets") or [] if n][:3]:
        try:
            sec = _common._get_json(["get", "secret", secret, "-n", ns, "-o", "json", "--show-managed-fields"])
            keys = sorted((sec.get("data") or {}).keys())
            smeta = sec.get("metadata") or {}
            del sec
            lines.append(
                f"Secret {secret} ({_written(ctx, smeta.get('creationTimestamp'), change_log.last_spec_write(smeta))}"
                f"): keys {keys} (values not read)"
            )
        except Exception as exc:  # noqa: BLE001 - a missing Secret is a fact to report
            lines.append(f"Secret {secret}: {collector.trim(str(exc), 160)}")
    return lines


def run_config_and_changes(ctx: AgentContext, cid: str) -> ToolResult:
    """config_and_changes: who wrote the component's spec and when, template revisions, and the ConfigMaps and
    Secrets it reads."""
    comp = _comp(ctx, cid)
    ns, kind, name = comp["namespace"], comp["kind"], comp["name"]
    lines: list[str] = []
    obj = _common._get_json(["get", kind.lower(), name, "-n", ns, "-o", "json", "--show-managed-fields"])
    meta = obj.get("metadata") or {}
    writes = change_log.spec_writes(meta)
    lines.append(
        f"{cid} created {_when(ctx, meta.get('creationTimestamp'))}; spec writers: "
        + (
            ", ".join(f"{w.get('manager')} {w.get('operation')} {_when(ctx, w.get('time'))}" for w in writes[-4:])
            or "none"
        )
    )
    for w in change_log.later_writes(meta)[-2:]:
        lines.append(f"fields written {_when(ctx, w['at'])}{write_note(ctx, w['at'])}: {', '.join(w['fields'][:6])}")
    errors: list[str] = []
    rev_lines, history = _revision_lines(ctx, comp, meta, errors)
    lines += rev_lines
    lines += _config_ref_lines(ctx, comp)
    try:
        lines += _stale_env_refs(ctx, comp, obj)
    except Exception as exc:  # noqa: BLE001 - an extra fact, not the tool's core
        logger.info("stale-config check for %s failed: %s", cid, exc)
    return ToolResult(lines=lines, raw={"revisions": len(history), "errors": errors})


def _env_refs(template_spec: dict) -> set[tuple[str, str]]:
    """(kind, name) of the ConfigMaps and Secrets a pod template reads into environment variables."""
    refs: set[tuple[str, str]] = set()
    for c in (template_spec.get("containers") or []) + (template_spec.get("initContainers") or []):
        for e in c.get("env") or []:
            src = e.get("valueFrom") or {}
            if (src.get("secretKeyRef") or {}).get("name"):
                refs.add(("secret", src["secretKeyRef"]["name"]))
            if (src.get("configMapKeyRef") or {}).get("name"):
                refs.add(("configmap", src["configMapKeyRef"]["name"]))
        for e in c.get("envFrom") or []:
            if (e.get("secretRef") or {}).get("name"):
                refs.add(("secret", e["secretRef"]["name"]))
            if (e.get("configMapRef") or {}).get("name"):
                refs.add(("configmap", e["configMapRef"]["name"]))
    return refs


def _stale_env_refs(ctx: AgentContext, comp: dict, workload: dict) -> list[str]:
    """ConfigMaps/Secrets read into env vars that were written after a running container started: environment
    values are read once at container start, so that container still holds the values from before the write."""
    tpl = ((workload.get("spec") or {}).get("template") or {}).get("spec") or {}
    refs = _env_refs(tpl)
    if not refs:
        return []
    ns = comp["namespace"]
    pods, _ = investigate.pod_details(_common._current_pod_names(ctx, comp), ns)
    starts = []
    for pod in pods[:2]:
        for cs in pod.get("containers") or []:
            started = parse_time(((cs.get("state") or {}).get("running") or {}).get("startedAt"))
            if started:
                starts.append((pod.get("name"), cs.get("name"), started))
    if not starts:
        return []
    out = []
    for kind, name in sorted(refs):
        meta = (
            _common._get_json(["get", kind, name, "-n", ns, "-o", "json", "--show-managed-fields"]).get("metadata")
        ) or {}
        written = parse_time(change_log.last_spec_write(meta))
        if written is None:
            continue
        # A rewrite after creation that any running container started before cannot be in that container's env,
        # however short the gap (timestamps have 1s resolution). Creation itself only counts with a clear gap, so
        # objects created alongside their workload at deploy time are not reported.
        rewritten = (seconds_between(written, parse_time(meta.get("creationTimestamp"))) or 0) > 0
        least = 0 if rewritten else change_log.MIN_CHANGE_SECONDS
        for pod, container, started in starts:
            after = seconds_between(written, started)
            if after is None or after < least:
                continue
            label = {"secret": "Secret", "configmap": "ConfigMap"}[kind]
            if after == 0:
                out.append(
                    f"{label} {name} (read into environment variables) was rewritten in the same second as container "
                    f"{container} of pod {pod} started (timestamps have 1s resolution): that container may still "
                    "hold the values from before the write"
                )
            else:
                out.append(
                    f"{label} {name} (read into environment variables) was written {after}s after container "
                    f"{container} of pod {pod} started: that container still holds the values from before the write"
                )
            break
    return out[:3]
