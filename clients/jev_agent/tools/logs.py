"""logs: a component's error lines, the components they name, and a crash's last lines."""

from __future__ import annotations

import logging

from clients.jev_agent import logfmt, roles
from clients.jev_agent.tools import _common
from clients.jev_agent.tools._common import _comp, _components, _warmup_crash
from clients.jev_agent.types import AgentContext, ToolResult
from clients.jev_diag import collector, derive, investigate

logger = logging.getLogger("all.jev_agent.tools")


def _components_named(ctx: AgentContext, cid: str, texts: list[str]) -> list[str]:
    """Components whose name, Service name, or backend-role synonym (redis/valkey, postgres, mongo, kafka, ...)
    appears in the given log text, as whole tokens. A text fact: which dependencies the errors talk about."""
    comps = _components(ctx)
    try:
        aliases = roles.name_aliases(comps)
    except Exception as exc:  # noqa: BLE001
        logger.info("component aliases unavailable: %s", exc)
        return []
    found: set[str] = set()
    for text in texts:
        found |= derive._mentions(text or "", aliases, cid)
    return sorted(found)


CRASH_TAIL_LINES = 120


def _crash_words(tail: list[str]) -> list[str]:
    """The error lines that say why a run ended: the first distinct error (usually the cause) and the last two
    (usually the consequences: abort, signal). Lines that differ only in numbers count once, so a dump of many
    similar lines does not push the cause out."""
    errs: list[str] = []
    seen: set[str] = set()
    for line in tail:
        if not collector.is_error_line(line):
            continue
        signature = collector.normalize_log_line(line)[0]
        if signature not in seen:
            seen.add(signature)
            errs.append(line)
    if not errs:
        return tail[-2:]
    return errs[:1] + errs[-2:] if len(errs) > 3 else errs


def _crash_tails(pods: list[dict], ns: str, ld: dict) -> tuple[dict, list[str], list[str], set[str]]:
    """(previous-run log tails by pod/container, crash lines, start-up crash lines, keys of the containers that
    crashed after warm-up) for the containers of the first two pods whose last run ended abnormally."""
    # A container whose last run ended abnormally: its final lines usually say why, whether or not the restart
    # looks like start-up churn, so fetch them when log_detail did not.
    previous = dict(ld.get("previous_container_logs") or {})
    crash_lines: list[str] = []
    warmup_lines: list[str] = []
    crashed_keys: set[str] = set()
    for pod in pods[:2]:
        for cs in pod.get("containers") or []:
            term = ((cs.get("last_state") or {}).get("terminated")) or {}
            code, reason = term.get("exitCode"), term.get("reason")
            if not term or (code in (0, None) and reason != "OOMKilled"):
                continue
            key = f"{pod.get('name')}/{cs.get('name')}"
            try:
                # deeper than log_detail's tail: a crash often dumps many lines after the error that caused it
                out = collector.run_kubectl(
                    ["logs", pod["name"], "-n", ns, "-c", cs["name"], "--previous", f"--tail={CRASH_TAIL_LINES}"],
                    timeout=30,
                )
                previous[key] = [collector.trim(logfmt.compact(ln), 200) for ln in out.splitlines()]
            except Exception:  # noqa: BLE001 - the previous run's logs may be gone
                previous.setdefault(key, [])
            last_words = " | ".join(_crash_words(previous[key])) or "none kept"
            warm = _warmup_crash(pod, cs)
            if warm:
                warmup_lines.append(
                    f"start-up crash, since recovered: previous run of {key} ended {reason or 'abnormally'} "
                    f"exit={code} {warm[0]}s after the pod started; running without restart for {warm[1]}s; "
                    f"its last error lines: {last_words}"
                )
                continue
            crashed_keys.add(key)
            crash_lines.append(
                f"previous run of {key} ended {reason or 'abnormally'} exit={code}; its last error lines: {last_words}"
            )
    return previous, crash_lines, warmup_lines, crashed_keys


def run_logs(ctx: AgentContext, cid: str) -> ToolResult:
    """logs: the component's error lines (ongoing or stopped), the components they name, and a crash's last lines."""
    comp = _comp(ctx, cid)
    ns = comp["namespace"]
    names = _common._current_pod_names(ctx, comp)
    pods, _ = investigate.pod_details(names, ns)
    ld = investigate.log_detail(names, ns, pods, rewrite=logfmt.compact)
    lines = [
        f"read {ld.get('total_lines', 0)} lines from {min(len(names), 2)} pod(s): {ld.get('error_lines', 0)} error-like "
        f"(still occurring {ld.get('error_lines_still_occurring', 0)}, stopped {ld.get('error_lines_stopped', 0)}); "
        f"telemetry-export error lines {ld.get('telemetry_export_error_lines', 0)}"
    ]
    all_samples = ld.get("error_samples") or []
    # log_detail records a failed fetch as a bracketed pseudo-line; report it as such, not as an application error.
    unavailable = [s for s in all_samples if "[logs unavailable for " in (s.get("line") or "")]
    samples = [s for s in all_samples if s not in unavailable]
    for s in unavailable[:1]:
        lines.append("log fetch failed: " + (s.get("line") or "").split("[logs unavailable for ", 1)[-1].rstrip("]"))
    previous, crash_lines, warmup_lines, crashed_keys = _crash_tails(pods, ns, ld)
    # Only errors still occurring, and the last words of a container that crashed after warm-up, link components;
    # stopped errors and start-up crashes are history (start-up churn), as in jev_diag's collector.
    named = _components_named(
        ctx,
        cid,
        [s.get("line") or "" for s in samples if s.get("state") != "stopped"]
        + [ln for key, tail in previous.items() if key in crashed_keys for ln in tail],
    )
    if named:
        lines.append("error lines name other components: " + ", ".join(named))
    lines += crash_lines[:2]
    for s in samples[: max(1, 4 - len(crash_lines[:2]))]:
        gap = f", every ~{s['median_gap_seconds']}s" if s.get("median_gap_seconds") is not None else ""
        lines.append(
            f"{s.get('state', '?')} x{s.get('count')} (first {s.get('first_seen_seconds_ago', '?')}s ago, last "
            f"{s.get('last_seen_seconds_ago', '?')}s ago{gap}): {s.get('line')}"
        )
    for r in (ld.get("repeated_failures") or [])[:1]:
        lines.append(
            f"repeats x{r.get('count')} over {r.get('span_seconds')}s with unchanged values "
            f"{r.get('constant_tokens')}: {r.get('line')}"
        )
    lines += warmup_lines[:1]
    if not crash_lines and not warmup_lines:
        for key, tail in list((ld.get("previous_container_logs") or {}).items())[:1]:
            lines.append(f"previous run of {key} ended with: " + " | ".join(tail[-3:]))
    if not samples and ld.get("tail"):
        lines.append("last lines: " + " | ".join(ld["tail"][-3:]))
    return ToolResult(lines=lines, raw=ld)
