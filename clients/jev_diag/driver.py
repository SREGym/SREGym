"""Jev diagnosis-only agent driver for SREGym.

Flow: wait for the conductor's diagnosis stage -> read app info -> collect the
cluster state deterministically (kubectl, logs, alerts) -> ask Jev which
component is the root cause -> submit a diagnosis text assembled in code.

Every step is recorded in a decision trace (``<prefix>_trace.jsonl``) and
rendered as a visualizer trajectory under ``<logs-dir>/trajectory/``. Summarize
a trace with ``python -m clients.jev_diag.trace <trace.jsonl>``.

Standalone use (no conductor): pass --namespace and --dry-run to collect and
build the Jev request without calling the API or submitting.
"""

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

from clients.harness.problem_id import resolve_problem_id  # noqa: E402
from clients.harness.token_usage import usage_metrics  # noqa: E402
from clients.jev_diag.classifier import (  # noqa: E402
    DEFAULT_STATE_TOKEN_BUDGET,
    JevDiagnoser,
    build_component_questions,
    fit_state_to_budget,
    make_client,
)
from clients.jev_diag.collector import (  # noqa: E402
    DEFAULT_LOG_TAIL,
    collect_snapshot,
    estimate_tokens,
    fetch_alerts_via_mcp,
    set_kubectl_observer,
)
from clients.jev_diag.investigate import collect_component_detail  # noqa: E402
from clients.jev_diag.trace import DecisionTrace  # noqa: E402
from clients.jev_diag.tree import DEFAULT_MAX_STEPS, IterativeDiagnoser  # noqa: E402
from sregym.env_file import load_env_file  # noqa: E402

logger = logging.getLogger("all.jev_diag.driver")

SUBMIT_TIMEOUT = 310  # the conductor may hold a submission for up to 300s


def run_preflight() -> None:
    """Fail fast when the TypeSafe key is missing or the API is unreachable."""
    load_env_file()
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        print("missing TYPESAFE_API_KEY")
        sys.exit(1)
    try:
        with make_client() as client:
            names = [m.name for m in client.models.list().models]
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        print(f"TypeSafe API check failed: {exc}")
        sys.exit(1)
    print(f"TypeSafe models available: {', '.join(names) or 'none listed'}")


def api_base_url() -> str:
    return f"http://{os.getenv('API_HOSTNAME', 'localhost')}:{os.getenv('API_PORT', '8000')}"


def wait_for_stage(timeout: int) -> str:
    """Poll /status until the conductor is in a submission stage or done."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            stage = requests.get(f"{api_base_url()}/status", timeout=10).json().get("stage")
        except Exception as exc:  # noqa: BLE001
            logger.debug("status not available yet: %s", exc)
            stage = None
        if stage in {"diagnosis", "mitigation", "done"}:
            return stage
        time.sleep(2)
    raise TimeoutError(f"Conductor did not reach a submission stage within {timeout}s")


def get_app_info() -> dict:
    response = requests.get(f"{api_base_url()}/get_app", timeout=30)
    response.raise_for_status()
    return response.json()


def submit_diagnosis(text: str) -> dict:
    response = requests.post(
        f"{api_base_url()}/submit", json={"stage": "diagnosis", "solution": text}, timeout=SUBMIT_TIMEOUT
    )
    if response.status_code >= 400:
        raise RuntimeError(f"submit failed with HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def collection_summary(snapshot) -> dict:
    """The part of the snapshot worth keeping in the trace: what code concluded per component."""
    return {
        "namespaces": snapshot.app.get("namespaces"),
        "components": {
            cid: {
                "kind": c.get("kind"),
                "healthy": c.get("healthy"),
                "signals": c.get("signals", []),
                "log_error_lines": c.get("log_error_lines", 0),
                "pods": len(c.get("pods", [])),
                "warning_events": len(c.get("warning_events", [])),
                "log_signals": len(c.get("log_signals", [])),
                "alerts": c.get("alerts", []),
            }
            for cid, c in snapshot.components.items()
        },
        "firing_alerts": [a.get("alertname") for a in snapshot.cluster.get("firing_alerts", [])],
        "nodes_with_problems": [n["name"] for n in snapshot.cluster.get("nodes", []) if n.get("problems")],
        "services_matching_no_workload": [s["name"] for s in snapshot.cluster.get("services_matching_no_workload", [])],
        "collection_errors": snapshot.errors,
        "state_tokens": estimate_tokens(snapshot.to_state()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnosis-only SRE agent using TypeSafe's Jev model")
    parser.add_argument("--logs-dir", default=os.environ.get("AGENT_LOGS_DIR", "./logs/jev_diag"))
    parser.add_argument("--problem-id", default=None, help="Artifact id (default: SREGYM_ARTIFACT_ID)")
    parser.add_argument(
        "--namespace",
        action="append",
        default=None,
        help="Standalone mode: application namespace(s) to inspect instead of asking the conductor",
    )
    parser.add_argument("--app-name", default=None, help="Standalone mode: application name for the state")
    parser.add_argument("--dry-run", action="store_true", help="Collect and build the request; skip Jev and submission")
    parser.add_argument("--no-submit", action="store_true", help="Call Jev but do not submit the diagnosis")
    parser.add_argument("--no-alerts", action="store_true", help="Skip the Prometheus MCP alert lookup")
    parser.add_argument("--no-logs", action="store_true", help="Skip pod log collection")
    parser.add_argument("--no-characterize", action="store_true", help="Skip the second (fault category) request")
    parser.add_argument(
        "--mode",
        choices=("tree", "oneshot"),
        default=os.environ.get("JEV_DIAG_MODE", "tree"),
        help="tree: iterative decision tree that investigates candidates (default); oneshot: two fixed requests",
    )
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="tree mode: investigation step budget")
    parser.add_argument(
        "--start-delay",
        type=float,
        default=float(os.environ.get("JEV_DIAG_START_DELAY", "120")),
        help="seconds to wait after the conductor hands over the application before reading the cluster, so the "
        "fault has time to surface in pods and logs (default 120; 0 disables; not applied in standalone mode)",
    )
    parser.add_argument("--log-tail", type=int, default=DEFAULT_LOG_TAIL)
    parser.add_argument("--state-token-budget", type=int, default=DEFAULT_STATE_TOKEN_BUDGET)
    parser.add_argument("--wait-timeout", type=int, default=600, help="Seconds to wait for the diagnosis stage")
    return parser.parse_args()


def main() -> None:
    load_env_file()
    args = parse_args()
    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    problem_id = resolve_problem_id(cli_problem_id=args.problem_id)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = logs_dir / f"jev_diag_{problem_id}_{stamp}"
    started = time.time()

    trace = DecisionTrace(
        prefix.with_name(prefix.name + "_trace.jsonl"),
        problem_id=problem_id,
        run_args={**vars(args), "typesafe_model": os.getenv("TYPESAFE_DEFAULT_MODEL", "jev-latest")},
    )
    set_kubectl_observer(trace.kubectl_observer())
    status = "error"
    results: dict = {"problem_id": problem_id, "timestamp": stamp, "success": False, "trace_path": str(trace.path)}
    try:
        status = run(args, trace, prefix, results, started)
    except SystemExit:
        status = "exit"
        raise
    except BaseException as exc:
        trace.record("run", "error", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc(limit=12))
        logger.error("jev_diag failed: %s", exc)
        raise
    finally:
        set_kubectl_observer(None)
        try:
            trajectory = trace.write_trajectory(logs_dir / "trajectory")
            results["trajectory_path"] = str(trajectory)
            logger.info("Trajectory written to %s", trajectory)
        except Exception as exc:  # noqa: BLE001 - never let trace rendering mask the run outcome
            logger.warning("Could not write trajectory: %s", exc)
        trace.close(status, duration_seconds=round(time.time() - started, 1))
        write_json(prefix.with_name(prefix.name + "_results.json"), results)


def run(args: argparse.Namespace, trace: DecisionTrace, prefix: Path, results: dict, started: float) -> str:
    """The agent proper. Returns the run status recorded in the trace."""
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
            stage = wait_for_stage(args.wait_timeout)
            info["stage"] = stage
        if stage != "diagnosis":
            logger.warning("Conductor is at stage %r; this agent only handles diagnosis. Exiting.", stage)
            return "skipped_stage"
        with trace.timed("setup", "get_app"):
            app_info = get_app_info()
        trace.record("setup", "app_info", source="conductor", app_info=app_info)
        if args.start_delay > 0:
            logger.info("Waiting %.0fs before reading the cluster so the fault has time to surface", args.start_delay)
            with trace.timed("setup", "start_delay", seconds=args.start_delay):
                time.sleep(args.start_delay)
    logger.info("Diagnosing %s in namespaces %s", app_info.get("app_name"), app_info.get("namespaces"))

    with trace.timed("collect", "snapshot", include_logs=not args.no_logs, log_tail=args.log_tail):
        snapshot = collect_snapshot(
            app_info,
            log_tail=args.log_tail,
            alerts_fetcher=None if (args.no_alerts or standalone) else fetch_alerts_via_mcp,
            include_logs=not args.no_logs,
        )
    trace.record("collect", "summary", **collection_summary(snapshot))
    write_json(
        prefix.with_name(prefix.name + "_snapshot.json"),
        {**snapshot.to_state(), "collection_errors": snapshot.errors},
    )

    if args.dry_run:
        state, trims = fit_state_to_budget(snapshot.to_state(), args.state_token_budget)
        questions = build_component_questions(snapshot)
        import msgspec

        wire = {k: msgspec.to_builtins(q) for k, q in questions.items()}
        trace.record(
            "jev",
            "budget_fit",
            label="select_component",
            budget=args.state_token_budget,
            tokens_before=estimate_tokens(snapshot.to_state()),
            tokens_after=estimate_tokens(state),
            applied=trims,
        )
        trace.record("jev", "dry_run", label="select_component", state_tokens=estimate_tokens(state), questions=wire)
        write_json(
            prefix.with_name(prefix.name + "_request_dry_run.json"), {"state": state, "questions": wire, "trims": trims}
        )
        logger.info("Dry run complete; artifacts under %s", prefix.parent)
        results["success"] = True
        return "dry_run"

    if args.mode == "tree":
        diagnoser = IterativeDiagnoser(
            fetch_detail=collect_component_detail,
            max_steps=args.max_steps,
            state_token_budget=args.state_token_budget,
            trace=trace,
        )
    else:
        diagnoser = JevDiagnoser(
            state_token_budget=args.state_token_budget, characterize=not args.no_characterize, trace=trace
        )
    try:
        diagnosis = diagnoser.diagnose(snapshot)
    finally:
        write_json(prefix.with_name(prefix.name + "_jev_requests.json"), diagnoser.requests)

    (prefix.with_name(prefix.name + "_diagnosis.txt")).write_text(diagnosis.text, encoding="utf-8")
    logger.info("Diagnosis:\n%s", diagnosis.text)

    submission = None
    if args.no_submit or standalone:
        trace.record("submit", "skipped", reason="--no-submit" if args.no_submit else "standalone")
    else:
        try:
            with trace.timed("submit", "response") as info:
                submission = submit_diagnosis(diagnosis.text)
                info["response"] = submission
        except Exception as exc:  # noqa: BLE001 - the diagnosis is still worth recording
            logger.error("Submission failed: %s", exc)
            results["submission_error"] = str(exc)
        else:
            logger.info("Submission accepted: %s", submission)

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
            "usage_metrics": usage_metrics(input_tokens=diagnosis.input_tokens, output_tokens=diagnosis.output_tokens),
        }
    )
    return "ok" if submission is not None or args.no_submit or standalone else "submit_failed"


if __name__ == "__main__":
    main()
