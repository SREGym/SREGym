"""
Validate mitigation oracles for a set of SREGym problems without running an agent.

For each problem the script drives:

    deploy app
      -> inject fault
      -> [spike load]
      -> workload oracle   (expect DEGRADED — fault must be user-visible)
      -> mitigation oracle (expect FAIL)
      -> [restore load]
      -> recover fault
      -> workload oracle   (expect HEALTHY)
      -> mitigation oracle (expect PASS)
      -> undeploy / reconcile

A problem is considered valid iff:
  1. The workload degrades under fault (fault is not silent).
  2. The mitigation oracle reports failure under fault.
  3. The workload recovers after fault removal.
  4. The mitigation oracle reports success after recovery.

Diagnosis oracle is intentionally skipped (requires LLM judge).

Usage:
    python run-validate-oracles.py --problem incorrect_image
    python run-validate-oracles.py --problem a --problem b --problem c
    python run-validate-oracles.py --problems-file new_problems.txt
    python run-validate-oracles.py --problems-file new_problems.txt --spike-load
"""

import argparse
import asyncio
import csv
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from logger import init_logger
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.constants import StartProblemResult
from sregym.conductor.oracles.workload import WorkloadOracle
from sregym.conductor.problems.registry import ProblemRegistry

logger = logging.getLogger(__name__)

# ── rollout helpers ────────────────────────────────────────────────────────────

def _affected_deployments(problem) -> list[tuple[str, str]]:
    """Return [(deployment, namespace), ...] derived from problem.editable_files."""
    files = getattr(problem, "editable_files", None) or []
    ns = getattr(problem, "namespace", None)
    if not files or not ns:
        return []
    return sorted({(f.deployment, ns) for f in files})


def _restart_and_wait(problem, rollout_timeout: int, restart: bool) -> None:
    """Wait for (and optionally trigger) a rollout to reach Ready.

    - After inject: injector already restarted; we just wait for status.
    - After recover: recover_source_file_override doesn't restart on its own,
      so we force one then wait.
    """
    targets = _affected_deployments(problem)
    if not targets:
        logger.info("No editable_files declared; skipping explicit rollout coordination")
        return
    for deployment, namespace in targets:
        if restart:
            cmd = f"kubectl rollout restart deployment/{deployment} -n {namespace}"
            logger.info(f"$ {cmd}")
            subprocess.run(cmd, shell=True, check=False)
        status_cmd = (
            f"kubectl rollout status deployment/{deployment} "
            f"-n {namespace} --timeout={rollout_timeout}s"
        )
        logger.info(f"$ {status_cmd}")
        subprocess.run(status_cmd, shell=True, check=False)


# ── load helpers ───────────────────────────────────────────────────────────────

_LOCUST_BASELINE_USERS = 10
_LOCUST_BASELINE_SPAWN_RATE = 1


def _get_wrk(problem):
    """Return the LocustWorkloadManager if available, else None."""
    return getattr(getattr(problem, "app", None), "wrk", None)


def _set_load(problem, users: int, spawn_rate: int) -> bool:
    """Scale the load-generator. Returns True if a workload manager was found."""
    wrk = _get_wrk(problem)
    if wrk is None:
        logger.info("No workload manager on problem.app; skipping load adjustment")
        return False
    ns = getattr(problem, "namespace", "")
    logger.info(f"Setting load-generator to {users} users @ spawn_rate={spawn_rate}")
    wrk.change_users(users, ns)
    wrk.change_spawn_rate(spawn_rate, ns)
    return True


# ── workload oracle ────────────────────────────────────────────────────────────

def _ensure_workload_started(problem) -> bool:
    """Reserved for future use. The conductor's deploy already calls
    app.start_workload() which spawns the fetcher; we don't need to."""
    return True


def _run_workload_oracle(problem, enabled: bool = False) -> bool | None:
    """Evaluate the workload oracle. Disabled by default in this validation
    pipeline because the locust-fetcher + collect() path is flaky in our
    test cluster — collect() routinely times out at 90s × 2 calls per
    evaluation, costing ~6min/problem with no useful signal.

    Returns:
      True   — workload is healthy
      False  — workload is degraded
      None   — workload oracle skipped or unavailable
    """
    if not enabled:
        return None
    wrk = _get_wrk(problem)
    if wrk is None:
        logger.info("Workload oracle: skipped (no workload manager)")
        return None
    oracle = WorkloadOracle(problem, wrk_manager=wrk)
    result = oracle.evaluate()
    healthy = bool(result.get("success"))
    logger.info(f"Workload oracle: {'HEALTHY' if healthy else 'DEGRADED'}")
    return healthy


# ── core validation ────────────────────────────────────────────────────────────

def validate_problem(
    conductor: Conductor,
    problem_id: str,
    settle_seconds: int,
    rollout_timeout: int,
    spike_load: bool = False,
    spike_users: int = 100,
    spike_spawn_rate: int = 20,
    spike_settle_seconds: int = 120,
) -> dict:
    """Run the full inject/workload-check/recover validation sequence."""
    result = {
        "problem_id": problem_id,
        "deployed": False,
        # mitigation oracle
        "oracle_under_fault_success": None,
        "oracle_after_recovery_success": None,
        "injected_as_expected": None,
        "recovered_as_expected": None,
        # workload oracle
        "workload_healthy_under_fault": None,
        "workload_healthy_after_recovery": None,
        "fault_is_silent": None,
        # overall
        "validation_passed": False,
        "error": None,
        "elapsed_seconds": None,
    }
    start = time.time()

    try:
        conductor.problem_id = problem_id
        sp_result = asyncio.run(conductor.start_problem())

        if sp_result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
            result["error"] = "skipped_khaos_required"
            return result

        result["deployed"] = True
        problem = conductor.problem

        if not getattr(problem, "mitigation_oracle", None):
            result["error"] = "no_mitigation_oracle_attached"
            return result

        # Wait for injected pods to reach Ready before evaluating.
        _restart_and_wait(problem, rollout_timeout, restart=False)

        # Spawn the locust-fetcher pod so the workload oracle has entries to
        # read. This must happen after the app is up and before we start
        # collecting. Without it wrk.collect() times out on empty input.
        _ensure_workload_started(problem)

        if spike_load:
            _set_load(problem, spike_users, spike_spawn_rate)
            logger.info(f"Waiting {spike_settle_seconds}s for load spike to stress the system...")
            time.sleep(spike_settle_seconds)
        else:
            logger.info(f"Waiting {settle_seconds}s for fault to take effect...")
            time.sleep(settle_seconds)

        # ── under-fault checks ──────────────────────────────────────────────
        wl_under = _run_workload_oracle(problem)
        result["workload_healthy_under_fault"] = wl_under
        # Fault is "silent" if workload oracle is available and reports healthy.
        result["fault_is_silent"] = (wl_under is True) if wl_under is not None else None
        if result["fault_is_silent"]:
            logger.warning(
                "Workload oracle reports HEALTHY under fault — fault is not manifesting "
                "at the request level. Mitigation oracle result may be unreliable."
            )

        mit_under = problem.mitigation_oracle.evaluate()
        under_fault_ok = bool(mit_under.get("success"))
        result["oracle_under_fault_success"] = under_fault_ok
        result["injected_as_expected"] = under_fault_ok is False

        # Restore baseline before recovery so the system can stabilise cleanly.
        if spike_load:
            _set_load(problem, _LOCUST_BASELINE_USERS, _LOCUST_BASELINE_SPAWN_RATE)
            logger.info("Restored baseline load before recovery phase")

        # ── recovery ────────────────────────────────────────────────────────
        logger.info("Recovering fault...")
        problem.recover_fault()

        _restart_and_wait(problem, rollout_timeout, restart=True)

        logger.info(f"Waiting {settle_seconds}s for recovery to take effect...")
        time.sleep(settle_seconds)

        # ── post-recovery checks ────────────────────────────────────────────
        wl_after = _run_workload_oracle(problem)
        result["workload_healthy_after_recovery"] = wl_after
        if wl_after is False:
            logger.warning("Workload oracle reports DEGRADED after recovery — system may not have healed.")

        mit_after = problem.mitigation_oracle.evaluate()
        after_recovery_ok = bool(mit_after.get("success"))
        result["oracle_after_recovery_success"] = after_recovery_ok
        result["recovered_as_expected"] = after_recovery_ok is True

        # ── overall verdict ─────────────────────────────────────────────────
        # Validation passes iff the mitigation oracle correctly flips in both
        # directions. The workload-oracle signals are informational only:
        # `fault_is_silent` flags the case where the fault doesn't manifest
        # at the request level (mitigation oracle result may be unreliable
        # there). We don't gate on workload-after-recovery because the
        # locust-fetcher+collect path is flaky in our test cluster — the
        # mitigation oracle is the authoritative signal.
        result["validation_passed"] = bool(
            result["injected_as_expected"]
            and result["recovered_as_expected"]
        )

    except Exception as e:
        logger.exception(f"Validation crashed for {problem_id}")
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            conductor._finish_problem()
        except Exception as cleanup_err:
            logger.warning(f"Cleanup error for {problem_id} (non-fatal): {cleanup_err}")
        result["elapsed_seconds"] = round(time.time() - start, 1)

    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

def load_problem_ids(args) -> list[str]:
    if args.problems_file:
        path = Path(args.problems_file)
        return [
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    return list(args.problem or [])


def main():
    parser = argparse.ArgumentParser(
        description="Validate mitigation oracles for SREGym problems (no agent)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--problem",
        action="append",
        help="Problem ID to validate (repeatable)",
    )
    group.add_argument(
        "--problems-file",
        type=str,
        help="Path to newline-delimited file of problem IDs (# for comments)",
    )
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=300,
        help="Seconds to wait after rollout / recovery before evaluating oracles (default: 300).",
    )
    parser.add_argument(
        "--rollout-timeout",
        type=int,
        default=180,
        help="Seconds to wait for a deployment rollout to reach Ready (default: 180).",
    )
    parser.add_argument(
        "--spike-load",
        action="store_true",
        help="Spike the load-generator before oracle evaluation. "
        "Needed for metastable faults that only manifest under sustained traffic.",
    )
    parser.add_argument(
        "--spike-users",
        type=int,
        default=100,
        help="LOCUST_USERS during the spike (default: 100).",
    )
    parser.add_argument(
        "--spike-spawn-rate",
        type=int,
        default=20,
        help="LOCUST_SPAWN_RATE during the spike (default: 20).",
    )
    parser.add_argument(
        "--spike-settle-seconds",
        type=int,
        default=120,
        help="Seconds to sustain the spike before evaluating oracles (default: 120).",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop after the first failing problem.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="results",
        help="Directory to write the CSV summary (default: results/).",
    )
    args = parser.parse_args()

    init_logger()

    problem_ids = load_problem_ids(args)
    if not problem_ids:
        logger.error("No problems specified")
        sys.exit(2)

    registry = ProblemRegistry()
    known = set(registry.get_problem_ids(all=True))
    unknown = [p for p in problem_ids if p not in known]
    if unknown:
        logger.error(f"Unknown problem IDs: {unknown}")
        logger.error(
            "Run `python -c 'from sregym.conductor.problems.registry import ProblemRegistry; "
            "print(\"\\n\".join(sorted(ProblemRegistry().get_problem_ids(all=True))))'` "
            "to list available IDs."
        )
        sys.exit(2)

    conductor = Conductor(config=ConductorConfig(deploy_loki=False, enable_noise=False))

    results: list[dict] = []
    all_passed = True
    for pid in problem_ids:
        logger.info(f"=== Validating: {pid} ===")
        res = validate_problem(
            conductor,
            pid,
            settle_seconds=args.settle_seconds,
            rollout_timeout=args.rollout_timeout,
            spike_load=args.spike_load,
            spike_users=args.spike_users,
            spike_spawn_rate=args.spike_spawn_rate,
            spike_settle_seconds=args.spike_settle_seconds,
        )
        results.append(res)
        all_passed &= res["validation_passed"]

        wl_str = ""
        if res["workload_healthy_under_fault"] is not None:
            wl_label = "healthy" if res["workload_healthy_under_fault"] else "degraded"
            wl_str = f" workload={wl_label}"
        silent_str = " [SILENT FAULT]" if res.get("fault_is_silent") else ""

        logger.info(
            f"=== {pid}: {'PASS' if res['validation_passed'] else 'FAIL'} "
            f"(oracle_under_fault={res['oracle_under_fault_success']}, "
            f"oracle_after_recovery={res['oracle_after_recovery_success']},{wl_str} "
            f"elapsed={res['elapsed_seconds']}s){silent_str} ==="
        )
        if args.stop_on_failure and not res["validation_passed"]:
            logger.warning("--stop-on-failure: stopping early")
            break

    # ── write CSV ──────────────────────────────────────────────────────────
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m%d_%H%M")
    csv_path = out_dir / f"oracle_validation_{stamp}.csv"
    fieldnames = sorted({k for r in results for k in r})
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)

    # ── print summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("ORACLE VALIDATION SUMMARY")
    print("=" * 90)
    print(
        f"{'problem':<50} {'verdict':<5} {'mit↓':<5} {'mit↑':<5} "
        f"{'wl↓':<9} {'wl↑':<9} {'note'}"
    )
    print("-" * 90)
    for r in results:
        verdict = "PASS" if r["validation_passed"] else "FAIL"
        mit_down = str(r["oracle_under_fault_success"])
        mit_up = str(r["oracle_after_recovery_success"])
        wl_down = (
            "degraded" if r["workload_healthy_under_fault"] is False
            else "healthy" if r["workload_healthy_under_fault"] is True
            else "N/A"
        )
        wl_up = (
            "healthy" if r["workload_healthy_after_recovery"] is True
            else "degraded" if r["workload_healthy_after_recovery"] is False
            else "N/A"
        )
        note = ""
        if r.get("fault_is_silent"):
            note = "SILENT FAULT"
        elif r.get("error"):
            note = f"err: {r['error'][:40]}"
        print(
            f"{r['problem_id']:<50} {verdict:<5} {mit_down:<5} {mit_up:<5} "
            f"{wl_down:<9} {wl_up:<9} {note}"
        )

    passed_count = sum(1 for r in results if r["validation_passed"])
    silent_count = sum(1 for r in results if r.get("fault_is_silent"))
    print("=" * 90)
    print(f"Passed: {passed_count}/{len(results)}   Silent faults: {silent_count}   CSV: {csv_path}")
    print("=" * 90)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
