from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.conductor.oracles.apps.frappe import workers as frappe
from sregym.conductor.oracles.mitigation import MitigationOracle


@pytest.fixture
def state(monkeypatch):
    healthy = Mock(return_value={"success": True})
    monkeypatch.setattr(MitigationOracle, "evaluate", healthy)
    job = Mock(return_value={"ok": True, "status": "finished", "result": 100})
    monkeypatch.setattr(frappe, "background_job_probe", job)
    app = SimpleNamespace(http=Mock(return_value=(200, '{"message":"pong"}')))
    oracle = frappe.FrappeWorkerHealthOracle(SimpleNamespace(app=app))
    return oracle, healthy, job


def test_missing_required_workload_prevents_probe(state):
    oracle, healthy, job = state
    healthy.return_value = {"success": False, "reason": "required_deployment_scaled_to_zero"}
    assert oracle.evaluate()["success"] is False
    oracle.problem.app.http.assert_not_called()
    job.assert_not_called()


def test_real_web_response_and_background_work_are_required(state):
    oracle, _, job = state
    assert oracle.evaluate()["success"] is True
    job.assert_called_once_with(oracle.problem.app)


@pytest.mark.parametrize("response", [(500, "error"), (200, '{"message":"fake"}')])
def test_unrelated_or_failed_web_response_is_rejected(state, response):
    oracle, _, job = state
    oracle.problem.app.http.return_value = response
    assert oracle.evaluate()["success"] is False
    job.assert_not_called()


def test_background_job_failure_is_rejected(state):
    oracle, _, job = state
    job.return_value = {"ok": False, "status": "failed"}
    assert oracle.evaluate()["success"] is False


def test_probe_transport_error_returns_failure(state):
    oracle, _, job = state
    job.side_effect = RuntimeError("worker unavailable")
    assert oracle.evaluate()["success"] is False


@pytest.mark.parametrize("staging", [False, True])
def test_removing_the_staging_step_remains_a_valid_alternative(staging):
    oracle = frappe.DevShmFrappeOracle(SimpleNamespace(shm_mount_path="/dev/shm"))
    pod = client.V1PodSpec(
        containers=[client.V1Container(name="long", args=["/dev/shm/rq-spool" if staging else "worker"])]
    )
    assert oracle._has_memory_backed_shm(pod) is not staging
