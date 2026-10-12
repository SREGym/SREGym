import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.gitea.application import GiteaApplicationOracle
from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle

READY_DEPLOYMENT = {
    "metadata": {"generation": 1},
    "spec": {"replicas": 1},
    "status": {
        "observedGeneration": 1,
        "replicas": 1,
        "updatedReplicas": 1,
        "readyReplicas": 1,
        "availableReplicas": 1,
    },
}


def oracle(monkeypatch):
    app = SimpleNamespace(slug="mattermost", auxiliary_deployments=(), scale_tier="replicated", members=3)
    app.command = Mock(return_value=json.dumps(READY_DEPLOYMENT))
    app.run_client, app.check_workflow = Mock(), Mock(return_value={"token": "b" * 32})
    result = PostgresApplicationOracle(SimpleNamespace(app=app))
    result.baseline = {"token": "a" * 32}
    result.volumes = {"data": "original"}
    result.volume_ids = Mock(return_value=result.volumes)
    result.verify_replication = Mock(return_value=None)
    monkeypatch.setattr(ServiceEndpointMitigationOracle, "evaluate", lambda self: {"success": True})
    return result, app


def test_recreated_storage_cannot_pass(monkeypatch):
    judge, app = oracle(monkeypatch)
    judge.volume_ids.return_value = {"data": "replacement"}
    verdict = judge.evaluate()
    assert verdict["success"] is False
    app.check_workflow.assert_not_called()


@pytest.mark.parametrize("oracle_class", [PostgresApplicationOracle, GiteaApplicationOracle])
@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        pytest.param([("data", "original", "Bound")], True, id="original"),
        pytest.param([("data", "original", "Bound"), ("backup", "extra", "Bound")], True, id="additional-backup"),
        pytest.param([("backup", "extra", "Bound")], False, id="original-missing"),
        pytest.param([("data", "replacement", "Bound"), ("backup", "extra", "Bound")], False, id="original-replaced"),
        pytest.param([("data", "original", "Pending"), ("backup", "extra", "Bound")], False, id="original-unbound"),
    ],
)
def test_original_volumes_must_survive_but_additional_volumes_are_allowed(monkeypatch, oracle_class, claims, expected):
    app = SimpleNamespace(slug="gitea", auxiliary_deployments=(), scale_tier="replicated", members=3)

    def command(*args):
        if args[:2] == ("get", "pvc"):
            return json.dumps(
                {
                    "items": [
                        {"metadata": {"name": name, "uid": uid}, "status": {"phase": phase}}
                        for name, uid, phase in claims
                    ]
                }
            )
        return json.dumps(READY_DEPLOYMENT)

    app.command = Mock(side_effect=command)
    app.run_client = Mock()
    app.check_workflow = Mock(return_value={"token": "b" * 32})
    judge = oracle_class(SimpleNamespace(app=app))
    judge.baseline = {"token": "a" * 32}
    judge.volumes = {"data": "original"}
    judge.verify_replication = Mock(return_value=None)
    monkeypatch.setattr(ServiceEndpointMitigationOracle, "evaluate", lambda self: {"success": True})

    result = judge.evaluate()

    assert result["success"] is expected
    if expected:
        app.run_client.assert_called_once()
        app.check_workflow.assert_called_once()
    else:
        assert result["reason"] == "persistent_volumes_replaced_or_missing"
        app.run_client.assert_not_called()
        app.check_workflow.assert_not_called()


def test_preserved_baseline_is_checked_before_fresh_writes(monkeypatch):
    judge, app = oracle(monkeypatch)
    app.run_client.side_effect = RuntimeError("Acknowledged attachment missing")
    assert judge.evaluate()["success"] is False
    app.check_workflow.assert_not_called()


def test_missing_worker_cannot_pass_with_healthy_frontend(monkeypatch):
    judge, app = oracle(monkeypatch)
    app.auxiliary_deployments = ("worker",)
    app.command.side_effect = [
        json.dumps(READY_DEPLOYMENT),
        json.dumps({"spec": {"replicas": 0}, "status": {}}),
    ]
    assert judge.evaluate()["success"] is False
    app.check_workflow.assert_not_called()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("metadata", "generation", 2),
        ("metadata", "deletionTimestamp", "2026-10-10T00:00:00Z"),
        ("status", "updatedReplicas", 0),
        ("status", "replicas", 2),
        ("status", "availableReplicas", 0),
    ],
)
def test_auxiliary_deployment_requires_current_rollout(monkeypatch, section, field, value):
    judge, app = oracle(monkeypatch)
    app.auxiliary_deployments = ("worker",)
    worker = deepcopy(READY_DEPLOYMENT)
    worker[section][field] = value
    app.command.side_effect = [json.dumps(READY_DEPLOYMENT), json.dumps(worker)]

    result = judge.evaluate()

    assert result["success"] is False
    assert result["reason"] == "required_deployment_not_rolled_out"
    assert result["detail"]["deployment"] == "worker"
    app.check_workflow.assert_not_called()


def test_current_auxiliary_rollout_reaches_business_checks(monkeypatch):
    judge, app = oracle(monkeypatch)
    app.auxiliary_deployments = ("worker",)

    assert judge.evaluate()["success"] is True
    app.run_client.assert_called_once_with("verify", **judge.baseline)
    app.check_workflow.assert_called_once_with()
