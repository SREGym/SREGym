from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.lite_ia import common
from sregym.generators.workload.incident_arena_ledger import summarize


def summary(*, correct=True, dropped=0, latency=10, critical_broken=False):
    return summarize(
        [
            {
                "sent_s": 210 + number,
                "driver": "critical" if number < 4 else "read",
                "ok": not (critical_broken and number < 4),
                "correct": correct,
                "dropped": number < dropped,
                "phase": "peak" if number % 2 else "trough",
                "latency_ms": latency,
            }
            for number in range(100)
        ]
    )


@pytest.fixture
def state(monkeypatch):
    sleeps = []
    monkeypatch.setattr(common.time, "sleep", sleeps.append)
    workload = Mock()
    workload.status.side_effect = [{"episode_id": "one", "latest_sent_s": point} for point in [200, 230, 350, 410]]
    workload.summary.return_value = summary()
    app = SimpleNamespace(wrk=workload, app_id="slack-spine", WORKLOAD_COMPLETION_TIMEOUT_S=60)
    oracle = common.LoadgenHealthOracle(SimpleNamespace(app=app))
    oracle.baseline = summary()
    return oracle, workload, sleeps


def test_healthy_complete_window_passes(state):
    oracle, workload, sleeps = state
    assert oracle.evaluate()["success"] is True
    assert sleeps == [30, 120, 60]
    workload.summary.assert_called_once_with(230.0, until_s=350.0)


@pytest.mark.parametrize(
    "measurement",
    [summary(correct=False), summary(dropped=99), summary(latency=60000), summary(critical_broken=True)],
)
def test_correctness_drops_latency_and_each_regular_driver_are_graded(state, measurement):
    oracle, workload, _ = state
    workload.summary.return_value = measurement
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "service_unhealthy"


@pytest.mark.parametrize("during", [1, 2, 3])
def test_episode_replacement_cannot_supply_a_healthy_measurement(state, during):
    oracle, workload, _ = state
    states = [{"episode_id": "one", "latest_sent_s": value} for value in [200, 230, 350, 410]]
    states[during]["episode_id"] = "two"
    workload.status.side_effect = states
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "loadgen_unavailable"
    workload.summary.assert_not_called()


def test_stalled_workload_does_not_reuse_old_records(state):
    oracle, workload, _ = state
    workload.status.side_effect = None
    workload.status.return_value = {"episode_id": "one", "latest_sent_s": 200}
    assert oracle.evaluate()["success"] is False
    workload.summary.assert_not_called()


def test_capture_requires_a_healthy_complete_baseline(state):
    oracle, workload, sleeps = state
    workload.status.side_effect = [{"episode_id": "one", "latest_sent_s": value} for value in [200, 260]]
    oracle.capture_baseline()
    workload.summary.assert_called_once_with(80.0, until_s=200.0)
    assert sleeps == [60]


def test_incorrect_baseline_stops_before_injection(state):
    oracle, workload, _ = state
    workload.summary.return_value = summary(correct=False)
    with pytest.raises(RuntimeError, match="baseline is not healthy"):
        oracle.capture_baseline()
    assert oracle.baseline is None


def test_intermittent_file_download_is_not_required_in_every_window(state):
    oracle, workload, _ = state
    oracle.baseline = deepcopy(oracle.baseline)
    oracle.baseline["by_driver"]["session_file_download"] = deepcopy(oracle.baseline["by_driver"]["read"])
    assert oracle.evaluate()["success"] is True


def test_missing_regular_driver_fails(state):
    oracle, workload, _ = state
    measurement = summary()
    del measurement["by_driver"]["critical"]
    workload.summary.return_value = measurement
    assert oracle.evaluate()["success"] is False
