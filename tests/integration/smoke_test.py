"""Integration smoke test for SREGym.

Exercises the full pipeline: cluster setup → app deploy → fault inject → evaluate → cleanup.
Uses misconfig_app_hotel_res (HotelReservation with buggy geo image) in mitigation-only mode.
Expects MitigationOracle to return success=False since the fault is not repaired.
"""

import asyncio

import pytest

from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.constants import StartProblemResult

PROBLEM_ID = "misconfig_app_hotel_res"
POLL_TIMEOUT_S = 600  # 10 minutes
POLL_INTERVAL_S = 5


async def _run_smoke_test():
    conductor = Conductor(config=ConductorConfig(deploy_loki=False))
    try:
        await _exercise_smoke_test(conductor)
    finally:
        # Embedded runs own these resources; leave no port-forward or verifier
        # behind even if a lifecycle assertion fails.
        if conductor._verifier_runtime is not None:
            conductor._verifier_runtime.cancel()
        conductor.mcp_server.stop_port_forward()
        conductor.k8s_proxy.stop()


async def _exercise_smoke_test(conductor):
    # 2. Select the problem
    conductor.problem_id = PROBLEM_ID
    # Keep this smoke test independent from a developer's local tasklist.yml.
    conductor.get_problem_stages = lambda: setattr(conductor, "tasklist", ["mitigation"])

    # 3. Deploy app and inject fault
    result = await conductor.start_problem()
    assert result == StartProblemResult.SUCCESS, f"start_problem returned {result}"
    assert conductor.submission_stage == "mitigation", (
        f"Expected stage 'mitigation', got '{conductor.submission_stage}'"
    )

    # 4. Submit a placeholder solution (we expect mitigation to fail)
    response = await conductor.submit("placeholder")
    assert response.get("status") == "accepted", f"submit response: {response}"

    # 5. Poll until evaluation completes
    elapsed = 0
    while conductor.submission_stage != "done":
        if elapsed >= POLL_TIMEOUT_S:
            pytest.fail(
                f"Timed out after {POLL_TIMEOUT_S}s waiting for evaluation to finish. "
                f"Stage: {conductor.submission_stage}"
            )
        await asyncio.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S

    # 6. Verify mitigation failed (fault was not repaired)
    assert "Mitigation" in conductor.results, f"Missing 'Mitigation' key in results: {conductor.results}"
    assert conductor.results["Mitigation"]["success"] is False, (
        f"Expected mitigation success=False, got: {conductor.results['Mitigation']}"
    )
    verdict = conductor.results["Mitigation"]
    assert verdict.get("failure_class") not in {"harness_error", "environment_error"}, verdict
    assert verdict.get("reason") != "verifier_execution_failed", verdict
    assert conductor._verifier_runtime.last_log_path.is_file(), "The private verifier must actually run"
    assert not conductor.results.get("cleanup_failed"), conductor.results.get("cleanup_error")


@pytest.mark.integration
def test_smoke_misconfig_app_hotel_res():
    """End-to-end smoke test: deploy HotelReservation, inject misconfig, evaluate mitigation."""
    asyncio.run(_run_smoke_test())
