"""v1.1 run: the same stages as v1's driver (wait for the diagnosis stage, collect, diagnose, submit), on the v1.1
modules. v1's driver hands over here for ``--mode v1.1`` and passes itself as ``host`` for the shared plumbing
(stage wait, app info, submission, artifact writing)."""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path
from types import ModuleType

from clients.jev_diag.trace import DecisionTrace
from clients.jev_diag.v1_1.classifier import build_component_questions, fit_state_to_budget
from clients.jev_diag.v1_1.collector import collect_snapshot, estimate_tokens, fetch_alerts_via_mcp
from clients.jev_diag.v1_1.investigate import collect_component_detail
from clients.jev_diag.v1_1.tree import IterativeDiagnoser

logger = logging.getLogger("all.jev_diag.v1_1.driver")


def run(
    args: argparse.Namespace, trace: DecisionTrace, prefix: Path, results: dict, started: float, host: ModuleType
) -> str:
    """The v1.1 agent proper. Returns the run status recorded in the trace."""
    standalone = bool(args.namespace)
    if standalone:
        app_info = {
            "app_name": args.app_name or args.namespace[0],
            "namespace": args.namespace[0],
            "namespaces": args.namespace,
            "descriptions": "",
        }
        trace.record("setup", "app_info", source="cli", app_info=app_info)
    else:
        with trace.timed("setup", "wait_for_stage") as info:
            stage = host.wait_for_stage(args.wait_timeout)
            info["stage"] = stage
        if stage != "diagnosis":
            logger.warning("Conductor is at stage %r; this agent only handles diagnosis. Exiting.", stage)
            return "skipped_stage"
        with trace.timed("setup", "get_app"):
            app_info = host.get_app_info()
        trace.record("setup", "app_info", source="conductor", app_info=app_info)
    logger.info("Diagnosing %s in namespaces %s", app_info.get("app_name"), app_info.get("namespaces"))

    snapshot = collect(args, app_info, trace, standalone, host)
    trace.record("collect", "summary", **host.collection_summary(snapshot))
    host.write_json(
        prefix.with_name(prefix.name + "_snapshot.json"),
        {
            **snapshot.to_state(include_private=True),
            "collection_errors": snapshot.errors,
            "collected_at": snapshot.collected_at,
        },
    )

    if args.dry_run:
        state, trims = fit_state_to_budget(snapshot.to_state(), args.state_token_budget)
        questions = build_component_questions(snapshot)
        import msgspec

        wire = {k: msgspec.to_builtins(q) for k, q in questions.items()}
        trace.record("jev", "dry_run", label="select_component", state_tokens=estimate_tokens(state), questions=wire)
        host.write_json(
            prefix.with_name(prefix.name + "_request_dry_run.json"), {"state": state, "questions": wire, "trims": trims}
        )
        results["success"] = True
        return "dry_run"

    diagnoser = make_diagnoser(args, trace)
    try:
        diagnosis = diagnoser.diagnose(snapshot)
    finally:
        host.write_json(prefix.with_name(prefix.name + "_jev_requests.json"), diagnoser.requests)

    (prefix.with_name(prefix.name + "_diagnosis.txt")).write_text(diagnosis.text, encoding="utf-8")
    logger.info("Diagnosis:\n%s", diagnosis.text)

    submission = None
    if args.no_submit or standalone:
        trace.record("submit", "skipped", reason="--no-submit" if args.no_submit else "standalone")
    else:
        try:
            with trace.timed("submit", "response") as info:
                submission = host.submit_diagnosis(diagnosis.text)
                info["response"] = submission
        except Exception as exc:  # noqa: BLE001 - the diagnosis is still worth recording
            logger.error("Submission failed: %s", exc)
            results["submission_error"] = str(exc)

    results.update(
        {
            "duration_seconds": round(time.time() - started, 1),
            "success": True,
            "component": diagnosis.component,
            "component_confidence": diagnosis.component_result.confidence,
            "component_probabilities": diagnosis.component_result.probabilities,
            "fault_category": diagnosis.category_result.choice if diagnosis.category_result else None,
            "fault_visible": diagnosis.fault_visible,
            "model": diagnosis.model,
            "mode": args.mode,
            "investigation": diagnosis.investigation,
            "trims": diagnosis.trims,
            "collection_errors": snapshot.errors,
            "submission": submission,
            "usage_metrics": host.usage_metrics(
                input_tokens=diagnosis.input_tokens, output_tokens=diagnosis.output_tokens
            ),
        }
    )
    return "ok" if submission is not None or args.no_submit or standalone else "submit_failed"


def collect(args: argparse.Namespace, app_info: dict, trace: DecisionTrace, standalone: bool, host: ModuleType):
    """Wait for the fault to surface, then read the cluster into a snapshot (v1.1's collection)."""
    baseline = wait(args, app_info, trace, standalone, host)
    return read_cluster(args, app_info, trace, standalone, baseline)


MIN_WAIT_SECONDS = 45  # never read the cluster sooner: a fault needs time to show in pods and logs
POLL_SECONDS = 15
TREND_SECONDS = 45  # the read comes at least this long after the baseline, so restarts and errors can recur


def abnormal_pods(sample: dict, baseline: dict) -> set[str]:
    """Pods that are not running cleanly now: not Running/Succeeded, or restarted since the baseline (restarts from
    the application's own start-up do not count)."""
    before = baseline.get("pods") or {}
    out = set()
    for key, pod in (sample.get("pods") or {}).items():
        restarts = (before.get(key) or {}).get("restarts") or {}
        grew = any(n > restarts.get(c, 0) for c, n in (pod.get("restarts") or {}).items())
        if pod.get("phase") not in ("Running", "Succeeded") or grew:
            out.add(key)
    return out


def wait(args: argparse.Namespace, app_info: dict, trace: DecisionTrace, standalone: bool, host: ModuleType):
    """Wait until the fault has surfaced; returns the trend baseline sample (or None).

    The baseline is taken after MIN_WAIT_SECONDS; then light samples (pods and events) every POLL_SECONDS. The
    cluster is read once the same set of abnormal pods shows in two samples in a row and at least TREND_SECONDS have
    passed since the baseline, and at the latest after the configured start delay (v1's fixed wait). Faults that
    show only in logs (pods stay healthy) wait the full delay. The polls are not recorded, so an offline replay
    serves the snapshot's own reads.
    """
    from clients.jev_diag.v1_1 import trend
    from clients.jev_diag.v1_1.collector import recording_paused

    delay = 0.0 if standalone else max(0.0, args.start_delay)
    if delay <= 0:
        return None
    namespaces = list(app_info.get("namespaces") or [app_info.get("namespace")])
    started = time.monotonic()
    with trace.timed("setup", "start_delay", seconds=delay, adaptive=True) as info:
        time.sleep(min(MIN_WAIT_SECONDS, delay))
        baseline = trend.collect_sample(namespaces)
        sampled_at = time.monotonic()
        previous: set[str] = set()
        while time.monotonic() - started + POLL_SECONDS <= delay:
            time.sleep(POLL_SECONDS)
            with recording_paused():
                current = abnormal_pods(trend.collect_sample(namespaces), baseline)
            if current and current == previous and time.monotonic() - sampled_at >= TREND_SECONDS:
                info["stable_abnormal_pods"] = len(current)
                break
            previous = current
        info["waited_seconds"] = round(time.monotonic() - started, 1)
    return baseline


def read_cluster(
    args: argparse.Namespace, app_info: dict, trace: DecisionTrace, standalone: bool, baseline: dict | None
):
    """Read the cluster once into v1.1's snapshot (no waiting)."""
    with trace.timed("collect", "snapshot", include_logs=not args.no_logs, log_tail=args.log_tail):
        snapshot = collect_snapshot(
            app_info,
            log_tail=args.log_tail,
            alerts_fetcher=None if (args.no_alerts or standalone) else fetch_alerts_via_mcp,
            include_logs=not args.no_logs,
            trend_baseline=baseline,
        )
    from clients.jev_diag.v1_1 import telemetry
    from clients.jev_diag.v1_1.derive import post_process

    rederive = False
    if not standalone:
        with trace.timed("collect", "observed_calls") as info:
            snapshot.cluster["_observed_calls"] = telemetry.observed_calls()
            info["edges"] = len(snapshot.cluster["_observed_calls"])
        rederive = bool(snapshot.cluster["_observed_calls"])
    if args.exec_probes:
        from clients.jev_diag.v1_1 import probes

        with trace.timed("collect", "broker_progress") as info:
            skipped: dict = {}
            stalled = probes.broker_progress(snapshot, None, skipped=skipped)
            info["stalled"] = stalled
            info["skipped"] = skipped
        rederive = rederive or bool(stalled)
    if rederive:
        post_process(snapshot)
    return snapshot


def make_diagnoser(args: argparse.Namespace, trace: DecisionTrace, client=None) -> IterativeDiagnoser:
    """v1.1's diagnoser; `client` overrides the decision model client (one per arm in experiments)."""
    probe = None
    if args.exec_probes or os.environ.get("JEV_DIAG_V1_1_PROBES", "1") != "0":
        # v1's read-only health command inside a healthy-looking data store that a victim's errors name (native
        # tools only, after a memory headroom check); on by default in v1.1
        from clients.jev_diag.v1_1.probes import backend_probe as probe
    return IterativeDiagnoser(
        client,
        fetch_detail=collect_component_detail,
        max_steps=args.max_steps,
        probe=probe,
        state_token_budget=args.state_token_budget,
        trace=trace,
    )
