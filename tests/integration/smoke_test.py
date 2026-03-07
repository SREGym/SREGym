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
    # 1. Create Conductor with Loki disabled (saves CI time)
    conductor = Conductor(config=ConductorConfig(deploy_loki=False))

    # 2. Select the problem
    conductor.problem_id = PROBLEM_ID

    # 3. Deploy app and inject fault
    result = await conductor.start_problem()
    assert result == StartProblemResult.SUCCESS, f"start_problem returned {result}"
    assert conductor.submission_stage == "mitigation", (
        f"Expected stage 'mitigation', got '{conductor.submission_stage}'"
    )

    # 4. Submit a placeholder solution (we expect mitigation to fail)
    response = await conductor.submit('```\nsubmit("placeholder")\n```')
    assert response.get("status") == "ok", f"submit response: {response}"

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


@pytest.mark.integration
def test_smoke_misconfig_app_hotel_res():
    """End-to-end smoke test: deploy HotelReservation, inject misconfig, evaluate mitigation."""
    asyncio.run(_run_smoke_test())
