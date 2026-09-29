import json
from types import SimpleNamespace
from unittest.mock import Mock

from sregym.conductor.oracles.saas import SaaSOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle


def oracle(monkeypatch):
    app = SimpleNamespace(slug="mattermost", auxiliary_deployments=(), scale_tier="replicated", members=3)
    app.command = Mock(return_value=json.dumps({"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}))
    app.run_client, app.check_workflow = Mock(), Mock(return_value={"token": "b" * 32})
    result = SaaSOracle(SimpleNamespace(app=app))
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


def test_preserved_baseline_is_checked_before_fresh_writes(monkeypatch):
    judge, app = oracle(monkeypatch)
    app.run_client.side_effect = RuntimeError("Acknowledged attachment missing")
    assert judge.evaluate()["success"] is False
    app.check_workflow.assert_not_called()


def test_missing_worker_cannot_pass_with_healthy_frontend(monkeypatch):
    judge, app = oracle(monkeypatch)
    app.auxiliary_deployments = ("worker",)
    app.command.side_effect = [
        json.dumps({"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}),
        json.dumps({"spec": {"replicas": 0}, "status": {}}),
    ]
    assert judge.evaluate()["success"] is False
    app.check_workflow.assert_not_called()
