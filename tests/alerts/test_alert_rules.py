"""
Test which Prometheus alerts fire for every problem in the registry.

Injects each fault, records all alerts that fire, then recovers.
Problems with known expected alerts are validated; problems without
known expectations are tested to discover what (if any) alerts fire.

Usage:
    uv run tests/alerts/test_alert_rules.py
    uv run tests/alerts/test_alert_rules.py --problem service_port_conflict_astronomy_shop
    uv run tests/alerts/test_alert_rules.py --list
    uv run tests/alerts/test_alert_rules.py --alert-timeout 300 --recover-timeout 300
    uv run tests/alerts/test_alert_rules.py --skip kubelet_crash,workload_imbalance
"""

import argparse
import contextlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field

PROMETHEUS_NODE_PORT = 32000

# Known expected alerts from PR #629 testing.
# Problems NOT in this map will still be tested — we just won't know
# what to expect, and the script will report whatever fires.
KNOWN_EXPECTED_ALERTS: dict[str, list[str]] = {
    "service_port_conflict_astronomy_shop": ["PendingPodsDetected", "PodSchedulingFailure"],
    "service_port_conflict_hotel_reservation": ["PendingPodsDetected", "PodSchedulingFailure"],
    "service_port_conflict_social_network": ["PendingPodsDetected", "PodSchedulingFailure"],
    "sidecar_port_conflict_astronomy_shop": ["PodStatusError", "KubePodNotReady"],
    "sidecar_port_conflict_hotel_reservation": ["PodStatusError", "KubePodNotReady"],
    "sidecar_port_conflict_social_network": ["PodStatusError", "KubePodNotReady"],
    "auth_miss_mongodb": ["PodStatusError", "KubePodNotReady"],
    "liveness_probe_misconfiguration_astronomy_shop": ["PodStatusError", "KubePodNotReady"],
    "liveness_probe_misconfiguration_hotel_reservation": ["PodStatusError", "KubePodNotReady"],
    "liveness_probe_misconfiguration_social_network": ["PodStatusError", "KubePodNotReady"],
    "liveness_probe_too_aggressive_astronomy_shop": ["PodStatusError", "KubePodNotReady"],
    "liveness_probe_too_aggressive_hotel_reservation": ["PodStatusError", "KubePodNotReady"],
    "liveness_probe_too_aggressive_social_network": ["PodStatusError", "KubePodNotReady"],
    "k8s_target_port-misconfig": ["ServiceEndpointDown"],
    "missing_configmap_hotel_reservation": ["PendingPodsDetected", "PodStatusError", "KubePodNotReady"],
    "missing_configmap_social_network": ["PendingPodsDetected", "PodStatusError", "KubePodNotReady"],
    "missing_service_astronomy_shop": ["ServiceEndpointDown"],
    "missing_service_hotel_reservation": ["PodStatusError", "KubePodNotReady"],
    "missing_service_social_network": ["ServiceEndpointDown"],
}

APP_NAMESPACES = {
    "astronomy-shop",
    "hotel-reservation",
    "social-network",
    "blueprint-hotel-reservation",
    "train-ticket",
    "tidb-cluster",
}


def detect_prometheus_url() -> str:
    try:
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "nodes",
                "-o",
                "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"http://{result.stdout.strip()}:{PROMETHEUS_NODE_PORT}"
    except Exception:
        pass
    return f"http://localhost:{PROMETHEUS_NODE_PORT}"


def get_firing_alerts(prometheus_url: str) -> list[dict]:
    """Return all currently firing alerts in app namespaces."""
    import requests

    try:
        resp = requests.get(f"{prometheus_url}/api/v1/alerts", timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [warn] Failed to query Prometheus: {e}", file=sys.stderr)
        return []

    alerts = resp.json().get("data", {}).get("alerts", [])
    return [
        a for a in alerts if a.get("state") == "firing" and a.get("labels", {}).get("namespace", "") in APP_NAMESPACES
    ]


def get_firing_alert_names(prometheus_url: str) -> set[str]:
    return {a["labels"]["alertname"] for a in get_firing_alerts(prometheus_url)}


def wait_for_any_alerts(
    prometheus_url: str,
    pre_existing: set[str],
    timeout: int,
    poll_interval: int = 15,
) -> set[str]:
    """Poll Prometheus and collect all NEW alerts that fire within the timeout window.

    Unlike wait_for_alerts, this doesn't stop early — it watches for the
    full duration to catch alerts that may fire at different times.
    Returns the set of all new alert names observed firing.
    """
    deadline = time.time() + timeout
    all_observed: set[str] = set()

    while time.time() < deadline:
        current = get_firing_alert_names(prometheus_url)
        new_alerts = current - pre_existing
        all_observed |= new_alerts

        elapsed = timeout - int(deadline - time.time())
        if new_alerts:
            print(f"  [{elapsed:>3d}s] Firing (new): {sorted(new_alerts)} | Cumulative: {sorted(all_observed)}")
        else:
            print(
                f"  [{elapsed:>3d}s] No new alerts | Cumulative: {sorted(all_observed) if all_observed else '(none)'}"
            )

        # If we have known expectations and they're all met, we can
        # still keep watching briefly for extras, but allow early exit
        # after at least 60s of observation
        if all_observed and elapsed >= 60:
            # Check if nothing new has appeared in recent polls
            pass

        time.sleep(poll_interval)

    return all_observed


def wait_for_clear(
    prometheus_url: str,
    timeout: int,
    poll_interval: int = 15,
) -> bool:
    """Wait until no alerts are firing in app namespaces."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        current = get_firing_alert_names(prometheus_url)
        elapsed = timeout - int(deadline - time.time())
        if not current:
            print(f"  [{elapsed:>3d}s] All alerts cleared.")
            return True
        print(f"  [{elapsed:>3d}s] Still firing: {sorted(current)}")
        time.sleep(poll_interval)

    return False


@dataclass
class TestResult:
    problem_id: str
    expected_alerts: list[str] | None  # None = no known expectations
    fired_alerts: list[str] = field(default_factory=list)
    missing_alerts: list[str] = field(default_factory=list)
    extra_alerts: list[str] = field(default_factory=list)
    has_coverage: bool = False
    passed: bool | None = None  # None = no expectations to validate
    error: str | None = None
    skipped: bool = False


def run_test(
    problem_id: str,
    expected_alerts: list[str] | None,
    prometheus_url: str,
    alert_timeout: int,
    recover_timeout: int,
) -> TestResult:
    from sregym.conductor.problems.registry import ProblemRegistry

    result = TestResult(problem_id=problem_id, expected_alerts=expected_alerts)

    # --- Snapshot pre-existing alerts ---
    pre_existing = get_firing_alert_names(prometheus_url)
    if pre_existing:
        print(f"  [warn] Pre-existing alerts: {sorted(pre_existing)}")

    # --- Instantiate problem ---
    try:
        registry = ProblemRegistry()
        problem = registry.get_problem_instance(problem_id)
    except Exception as e:
        result.error = f"Failed to instantiate: {e}"
        return result

    # --- Inject fault ---
    try:
        print("  Injecting fault...")
        problem.inject_fault()
    except Exception as e:
        result.error = f"Failed to inject fault: {e}"
        with contextlib.suppress(Exception):
            problem.recover_fault()
        return result

    # --- Watch for alerts ---
    print(f"  Watching for alerts (timeout={alert_timeout}s)...")
    observed = wait_for_any_alerts(prometheus_url, pre_existing, alert_timeout)

    result.fired_alerts = sorted(observed)
    result.has_coverage = len(observed) > 0

    if expected_alerts is not None:
        expected_set = set(expected_alerts)
        result.missing_alerts = sorted(expected_set - observed)
        result.extra_alerts = sorted(observed - expected_set)
        result.passed = expected_set.issubset(observed)
    else:
        # No known expectations — just record what fired
        result.passed = None

    # --- Recover fault ---
    try:
        print("  Recovering fault...")
        problem.recover_fault()
    except Exception as e:
        print(f"  [warn] Recovery failed: {e}", file=sys.stderr)

    # --- Wait for alerts to clear ---
    print(f"  Waiting for alerts to clear (timeout={recover_timeout}s)...")
    cleared = wait_for_clear(prometheus_url, recover_timeout)
    if not cleared:
        remaining = get_firing_alert_names(prometheus_url)
        print(f"  [warn] Alerts still firing after recovery: {sorted(remaining)}")

    return result


def print_summary(results: list[TestResult]) -> None:
    tested = [r for r in results if not r.skipped]
    skipped = [r for r in results if r.skipped]
    errored = [r for r in tested if r.error]
    with_coverage = [r for r in tested if r.has_coverage and not r.error]
    no_coverage = [r for r in tested if not r.has_coverage and not r.error]
    validated = [r for r in tested if r.passed is not None]
    passed = [r for r in validated if r.passed]

    print(f"\n{'=' * 70}")
    print(" TEST SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total problems:    {len(results)}")
    print(f"  Tested:            {len(tested)}")
    print(f"  Skipped:           {len(skipped)}")
    print(f"  Errors:            {len(errored)}")
    print(f"{'=' * 70}")
    print(
        f"  Alert coverage:    {len(with_coverage)}/{len(tested) - len(errored)} problems triggered at least one alert"
    )
    print(f"  No alert coverage: {len(no_coverage)}/{len(tested) - len(errored)} problems triggered NO alerts")
    if validated:
        print(f"  Validated:         {len(passed)}/{len(validated)} matched known expectations")
    print(f"{'=' * 70}")

    # --- Problems with alert coverage ---
    if with_coverage:
        print(f"\n  ALERT COVERAGE ({len(with_coverage)} problems):")
        for r in with_coverage:
            if r.expected_alerts is not None:
                status = "PASS" if r.passed else "FAIL"
                extra = ""
                if r.missing_alerts:
                    extra += f" missing={r.missing_alerts}"
                if r.extra_alerts:
                    extra += f" extra={r.extra_alerts}"
                print(f"    [{status}] {r.problem_id}: {r.fired_alerts}{extra}")
            else:
                print(f"    [????] {r.problem_id}: {r.fired_alerts}")

    # --- Problems WITHOUT alert coverage (key output for gap analysis) ---
    if no_coverage:
        print(f"\n  NO ALERT COVERAGE ({len(no_coverage)} problems):")
        for r in no_coverage:
            print(f"    [ -- ] {r.problem_id}")

    # --- Errors ---
    if errored:
        print(f"\n  ERRORS ({len(errored)} problems):")
        for r in errored:
            print(f"    [ERR ] {r.problem_id}: {r.error}")

    # --- Skipped ---
    if skipped:
        print(f"\n  SKIPPED ({len(skipped)} problems):")
        for r in skipped:
            print(f"    [SKIP] {r.problem_id}")

    print(f"\n{'=' * 70}\n")


def main():
    parser = argparse.ArgumentParser(description="Test SREGym Prometheus alert rules for every problem")
    parser.add_argument(
        "--problem",
        type=str,
        default=None,
        help="Run a single problem by ID",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all problems and their known expected alerts, then exit",
    )
    parser.add_argument(
        "--alert-timeout",
        type=int,
        default=240,
        help="Max seconds to wait for alerts after fault injection (default: 240)",
    )
    parser.add_argument(
        "--recover-timeout",
        type=int,
        default=240,
        help="Max seconds to wait for alerts to clear after recovery (default: 240)",
    )
    parser.add_argument(
        "--skip",
        type=str,
        default=None,
        help="Comma-separated list of problem IDs to skip",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to write JSON results",
    )
    args = parser.parse_args()

    from sregym.conductor.problems.registry import ProblemRegistry

    registry = ProblemRegistry()
    all_problem_ids = registry.get_problem_ids(all=True)

    if args.list:
        print(f"All problems ({len(all_problem_ids)}):\n")
        for pid in sorted(all_problem_ids):
            expected = KNOWN_EXPECTED_ALERTS.get(pid)
            if expected:
                print(f"  {pid}: {expected}")
            else:
                print(f"  {pid}: (no known expectations)")
        return

    skip_set = set()
    if args.skip:
        skip_set = {s.strip() for s in args.skip.split(",")}

    # Determine which problems to test
    if args.problem:
        if args.problem not in all_problem_ids:
            print(f"Error: '{args.problem}' not in problem registry.", file=sys.stderr)
            sys.exit(1)
        problem_ids = [args.problem]
    else:
        problem_ids = all_problem_ids

    prometheus_url = detect_prometheus_url()
    print(f"Prometheus: {prometheus_url}")
    print(f"Problems to test: {len(problem_ids)} (skipping {len(skip_set)})\n")

    results: list[TestResult] = []

    for i, problem_id in enumerate(problem_ids, 1):
        expected = KNOWN_EXPECTED_ALERTS.get(problem_id)
        has_expectations = expected is not None
        label = f"expected={expected}" if has_expectations else "no known expectations"

        print(f"\n{'─' * 70}")
        print(f" [{i}/{len(problem_ids)}] {problem_id}")
        print(f"   {label}")
        print(f"{'─' * 70}")

        if problem_id in skip_set:
            r = TestResult(problem_id=problem_id, expected_alerts=expected, skipped=True)
            results.append(r)
            print("  SKIPPED")
            continue

        result = run_test(
            problem_id=problem_id,
            expected_alerts=expected,
            prometheus_url=prometheus_url,
            alert_timeout=args.alert_timeout,
            recover_timeout=args.recover_timeout,
        )
        results.append(result)

        if result.error:
            print(f"  Result: ERROR — {result.error}")
        elif result.passed is True:
            print(f"  Result: PASS — fired {result.fired_alerts}")
        elif result.passed is False:
            print(f"  Result: FAIL — fired {result.fired_alerts}, missing {result.missing_alerts}")
        else:
            if result.has_coverage:
                print(f"  Result: DISCOVERED alerts {result.fired_alerts}")
            else:
                print("  Result: NO ALERTS fired")

    print_summary(results)

    if args.output:
        out = []
        for r in results:
            d = r.__dict__.copy()
            # Convert None to null-friendly for JSON
            out.append(d)
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Results written to {args.output}")

    # Exit code: 1 if any validated problem failed, 0 otherwise
    validated = [r for r in results if r.passed is not None]
    sys.exit(0 if all(r.passed for r in validated) else 1)


if __name__ == "__main__":
    main()
