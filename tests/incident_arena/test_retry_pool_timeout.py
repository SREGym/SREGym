from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.apps.slack_spine.retry_storm import RetryStormMitigationOracle
from sregym.conductor.problems.apps.slack_spine.retry_storm import RetryStormCollapseIA


@pytest.fixture
def oracle(offline_port_cluster):
    problem = RetryStormCollapseIA()
    problem.admin_all = Mock(
        side_effect=lambda role, path: {
            role: {
                "mesh": {"retries": 1, "perTryTimeoutMs": 750, "backoffMs": 50, "breakerEnabled": False},
                "db": {"pool_size": 20, "max_overflow": 10, "pool_timeout_s": problem.pool_timeout},
            }
        }
    )
    result = RetryStormMitigationOracle(problem)
    result._container_env = Mock(
        return_value=(
            {
                "AUTHZ_CHECK": "1",
                "WORKSPACE_POLICY_CHECK": "1",
                "SETTINGS_UNCACHED": "1",
                "SETTINGS_BASE_HOLD_MS": "100",
            },
            1,
        )
    )
    return result


@pytest.mark.parametrize("timeout", [0.01, 0.1, 0.25, 0.5, 1.0, 30.0])
def test_bounded_pool_wait_is_not_rejected_before_behavioral_replay(oracle, timeout):
    oracle.problem.pool_timeout = timeout
    assert oracle._policy_outside_envelope() is None


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan")])
def test_invalid_pool_wait_still_fails(oracle, timeout):
    oracle.problem.pool_timeout = timeout
    assert oracle._policy_outside_envelope()["reason"] == "backend_pool_outside_safe_envelope"


def test_short_pool_wait_must_still_survive_the_replayed_trigger(oracle):
    oracle.problem.pool_timeout = 0.5
    oracle._baseline_deployments = {"svc-workspace"}
    oracle._wait_for_healthy = Mock(side_effect=[None, oracle.fail("traffic_did_not_recover")])
    oracle.problem._trigger = Mock()
    result = oracle.evaluate()
    oracle.problem._trigger.assert_called_once_with(oracle.problem.trigger_seconds)
    assert result["reason"] == "did_not_recover_after_trigger"


def test_short_pool_wait_does_not_allow_disabled_policy_checks(oracle):
    oracle.problem.pool_timeout = 0.5
    oracle._container_env.return_value = ({"AUTHZ_CHECK": "0", "WORKSPACE_POLICY_CHECK": "0"}, 1)
    assert oracle._policy_outside_envelope()["reason"] == "policy_check_disabled"
