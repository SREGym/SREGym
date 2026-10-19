"""v2 run: the same stages as v1's driver (wait for the diagnosis stage, collect, diagnose, submit), on the v2
modules. v1's driver hands over here for ``--mode v2`` and passes itself as ``host`` for the shared plumbing
(stage wait, app info, submission, artifact writing)."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from types import ModuleType

from clients.jev_diag.trace import DecisionTrace
from clients.jev_diag.v2.classifier import build_component_questions, fit_state_to_budget
from clients.jev_diag.v2.collector import collect_snapshot, estimate_tokens, fetch_alerts_via_mcp
from clients.jev_diag.v2.investigate import collect_component_detail
from clients.jev_diag.v2.tree import IterativeDiagnoser

logger = logging.getLogger("all.jev_diag.v2.driver")


def run(
    args: argparse.Namespace, trace: DecisionTrace, prefix: Path, results: dict, started: float, host: ModuleType
) -> str:
    """The v2 agent proper. Returns the run status recorded in the trace."""
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

    delay = 0.0 if standalone else max(0.0, args.start_delay)
    interval = (
        args.trend_interval if args.trend_interval is not None else (0.0 if standalone else host.DEFAULT_TREND_INTERVAL)
    )
    interval = max(0.0, interval)
    if delay - interval > 0:
        logger.info("Waiting %.0fs before reading the cluster so the fault has time to surface", delay)
        with trace.timed("setup", "start_delay", seconds=delay - interval):
            time.sleep(delay - interval)
    baseline = None
    if interval > 0:
        from clients.jev_diag.v2 import trend

        with trace.timed("collect", "trend_baseline", interval=interval):
            baseline = trend.collect_sample(list(app_info.get("namespaces") or [app_info.get("namespace")]))
        time.sleep(interval)

    with trace.timed("collect", "snapshot", include_logs=not args.no_logs, log_tail=args.log_tail):
        snapshot = collect_snapshot(
            app_info,
            log_tail=args.log_tail,
            alerts_fetcher=None if (args.no_alerts or standalone) else fetch_alerts_via_mcp,
            include_logs=not args.no_logs,
            trend_baseline=baseline,
        )
    if args.exec_probes:
        from clients.jev_diag.v2 import probes
        from clients.jev_diag.v2.derive import post_process

        with trace.timed("collect", "broker_progress") as info:
            skipped: dict = {}
            stalled = probes.broker_progress(snapshot, None, skipped=skipped)
            info["stalled"] = stalled
            info["skipped"] = skipped
        if stalled:
            post_process(snapshot)
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

    probe = None
    if args.exec_probes:
        from clients.jev_diag.v2.probes import backend_probe as probe
    diagnoser = IterativeDiagnoser(
        fetch_detail=collect_component_detail,
        max_steps=args.max_steps,
        probe=probe,
        state_token_budget=args.state_token_budget,
        trace=trace,
    )
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
