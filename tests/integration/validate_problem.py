"""End-to-end validation for a single SREGym problem.

A contributed problem must survive the full lifecycle that an agent exercises:

    deploy app -> inject fault -> mitigation oracle FAILS
                -> recover fault -> mitigation oracle PASSES

This script runs exactly that sequence for one problem ID and exits non-zero if
any stage misbehaves, so CI can gate PRs that add or modify problems. A Markdown
summary is written to --summary for the PR-comment step of the workflow.

Usage:
    uv run python tests/integration/validate_problem.py --problem <problem_id>

Example:
    uv run python tests/integration/validate_problem.py --problem incorrect_image
"""

import argparse
import logging
import sys
import time

from logger import init_logger
from sregym.conductor.conductor import Conductor, ConductorConfig

logger = logging.getLogger("all.sregym.validate_problem")

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

_ICON = {PASS: "✅", FAIL: "❌", SKIP: "⏭️"}

# Defaults; overridable via CLI flags.
INJECT_TIMEOUT_S = 300  # how long to wait for the oracle to notice the fault
RECOVER_TIMEOUT_S = 600  # how long to wait for the oracle to confirm recovery
POLL_INTERVAL_S = 15


class Stage:
    """A single validation step and its outcome."""

    def __init__(self, name: str):
        self.name = name
        self.status = SKIP
        self.detail = "not run"


class ValidationError(Exception):
    """Raised when a stage does not behave as a correct problem requires."""


def _poll_oracle(oracle, expect_success: bool, timeout_s: int, poll_interval_s: int):
    """Evaluate the oracle until ``success == expect_success`` or the timeout elapses.

    Returns a tuple of (matched, checks, last_result).
    """
    deadline = time.monotonic() + timeout_s
    checks = 0
    last: dict = {}
    while True:
        checks += 1
        try:
            last = oracle.evaluate() or {}
        except Exception as e:
            logger.exception("mitigation_oracle.evaluate() raised")
            last = {"success": None, "error": f"{type(e).__name__}: {e}"}
        logger.info(f"Oracle check #{checks}: {last}")
        if last.get("success") is expect_success:
            return True, checks, last
        if time.monotonic() >= deadline:
            return False, checks, last
        time.sleep(poll_interval_s)


def _mark_failure(stages: dict, message: str):
    """Mark the first not-yet-passed stage as failed (the point of failure)."""
    for stage in stages.values():
        if stage.status == SKIP:
            stage.status = FAIL
            stage.detail = message
            return


def validate(problem_id: str, inject_timeout: int, recover_timeout: int, poll_interval: int):
    """Run the full deploy/inject/recover lifecycle. Returns (passed, stages)."""
    stages = {
        "resolve": Stage("Resolve problem in registry"),
        "deploy": Stage("Deploy application"),
        "inject": Stage("Inject fault"),
        "oracle_fail": Stage("Oracle fails after fault injection"),
        "recover": Stage("Recover fault"),
        "oracle_pass": Stage("Oracle passes after recovery"),
    }

    conductor = None
    try:
        conductor = Conductor(config=ConductorConfig(deploy_loki=False))

        # --- Resolve the problem ----------------------------------------------
        logger.info(f"[STAGE] Resolving problem '{problem_id}'")
        registry = conductor.problems
        if problem_id not in registry.PROBLEM_REGISTRY:
            raise ValidationError(
                f"'{problem_id}' is not registered in sregym/conductor/problems/registry.py. "
                f"Register the problem before it can be validated."
            )

        conductor.problem_id = problem_id
        problem = registry.get_problem_instance(problem_id)
        conductor.problem = problem
        conductor.app = problem.app

        if problem.requires_khaos() and conductor.kubectl.is_emulated_cluster():
            raise ValidationError(
                "this problem requires Khaos / a real (non-emulated) cluster for fault "
                "injection and cannot be validated on the CI kind cluster. Validate it "
                "manually on a supported cluster and have a maintainer confirm."
            )

        oracle = getattr(problem, "mitigation_oracle", None)
        if oracle is None:
            raise ValidationError(
                "problem has no mitigation_oracle attached. The validation workflow needs "
                "one to verify that the fault is detectable and that recovery restores health."
            )

        stages["resolve"].status = PASS
        stages["resolve"].detail = f"{type(problem).__name__} · app `{problem.app.name}`"

        # --- Deploy the application -------------------------------------------
        logger.info("[STAGE] Deploying application")
        conductor.dependency_check(["kubectl", "helm", "docker"])
        conductor.fix_kubernetes()
        conductor.undeploy_app()  # clear any leftovers from a previous run
        conductor.deploy_app()
        stages["deploy"].status = PASS
        stages["deploy"].detail = f"`{problem.app.name}` deployed to namespace `{problem.namespace}`"

        # --- Inject the fault -------------------------------------------------
        logger.info("[STAGE] Injecting fault")
        problem.inject_fault()
        stages["inject"].status = PASS
        stages["inject"].detail = "inject_fault() completed without error"

        # --- The oracle must now FAIL (the fault is live) ---------------------
        logger.info("[STAGE] Verifying the oracle detects the injected fault")
        matched, checks, result = _poll_oracle(oracle, False, inject_timeout, poll_interval)
        if not matched:
            raise ValidationError(
                f"the mitigation oracle still reports success {inject_timeout}s after injection "
                f"({checks} check(s)). The injected fault did not break the application, or the "
                f"attached oracle cannot detect it. Last result: {result}"
            )
        stages["oracle_fail"].status = PASS
        stages["oracle_fail"].detail = f"oracle reported failure after {checks} check(s)"

        # --- Recover the fault ------------------------------------------------
        logger.info("[STAGE] Recovering fault")
        problem.recover_fault()
        stages["recover"].status = PASS
        stages["recover"].detail = "recover_fault() completed without error"

        # --- The oracle must now PASS (recovery restored health) --------------
        logger.info("[STAGE] Verifying the oracle confirms recovery")
        matched, checks, result = _poll_oracle(oracle, True, recover_timeout, poll_interval)
        if not matched:
            raise ValidationError(
                f"the mitigation oracle still reports failure {recover_timeout}s after recovery "
                f"({checks} check(s)). recover_fault() did not restore the application to a "
                f"healthy state. Last result: {result}"
            )
        stages["oracle_pass"].status = PASS
        stages["oracle_pass"].detail = f"oracle reported success after {checks} check(s)"

        return True, stages

    except ValidationError as e:
        logger.error(f"Validation failed: {e}")
        _mark_failure(stages, str(e))
        return False, stages
    except Exception as e:  # noqa: BLE001 - any crash is a validation failure
        logger.exception("Validation crashed with an unexpected error")
        _mark_failure(stages, f"unexpected error: {type(e).__name__}: {e}")
        return False, stages
    finally:
        # Best-effort teardown so the script is re-runnable locally.
        try:
            if conductor is not None and conductor.problem is not None:
                conductor.problem.app.cleanup()
        except Exception:  # noqa: BLE001
            logger.warning("App cleanup after validation failed", exc_info=True)


def write_summary(path: str, problem_id: str, passed: bool, stages: dict):
    """Write a Markdown summary for the PR comment / artifact."""
    verdict = "✅ **PASSED**" if passed else "❌ **FAILED**"
    lines = [
        "<!-- problem-validation -->",
        f"## 🧪 Problem Validation — `{problem_id}`",
        "",
        f"**Result:** {verdict}",
        "",
    ]
    if passed:
        lines.append(
            "The problem completed the full lifecycle: the app deployed, the mitigation "
            "oracle detected the injected fault, and `recover_fault()` restored the app "
            "to a healthy state. Human review is still required."
        )
    else:
        lines.append(
            "The problem did **not** pass end-to-end validation. See the failing stage "
            "below and the workflow logs for details."
        )
    lines += [
        "",
        "| Stage | Status | Detail |",
        "|-------|:------:|--------|",
    ]
    for stage in stages.values():
        detail = stage.detail.replace("|", "\\|").replace("\n", " ")
        if len(detail) > 400:
            detail = detail[:397] + "..."
        lines.append(f"| {stage.name} | {_ICON[stage.status]} | {detail} |")
    lines += [
        "",
        "_Lifecycle: deploy app → inject fault → oracle fails → recover fault → oracle passes._",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Validate a single SREGym problem end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--problem", required=True, help="Registered problem ID to validate")
    parser.add_argument("--summary", default="validation-summary.md", help="Path for the Markdown summary")
    parser.add_argument(
        "--inject-timeout",
        type=int,
        default=INJECT_TIMEOUT_S,
        help="Seconds to wait for the oracle to detect the fault",
    )
    parser.add_argument(
        "--recover-timeout",
        type=int,
        default=RECOVER_TIMEOUT_S,
        help="Seconds to wait for the oracle to confirm recovery",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=POLL_INTERVAL_S,
        help="Seconds between oracle checks",
    )
    args = parser.parse_args()

    init_logger()
    logger.info(f"Starting validation for problem: {args.problem}")
    start = time.time()

    passed, stages = validate(args.problem, args.inject_timeout, args.recover_timeout, args.poll_interval)

    elapsed = time.time() - start
    write_summary(args.summary, args.problem, passed, stages)

    print("\n" + "=" * 64)
    print(f"PROBLEM VALIDATION — {args.problem}")
    print("=" * 64)
    for stage in stages.values():
        print(f"  {_ICON[stage.status]}  {stage.name}: {stage.detail}")
    print("-" * 64)
    print(f"  Result: {'PASSED' if passed else 'FAILED'}  ({elapsed:.0f}s)")
    print(f"  Summary written to: {args.summary}")
    print("=" * 64)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
