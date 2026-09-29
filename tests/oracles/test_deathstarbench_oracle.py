import json
from types import SimpleNamespace
from unittest.mock import Mock

from sregym.conductor.oracles.deathstarbench import ScaledServiceEndpointOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle


def test_scaled_oracle_never_passes_without_a_captured_baseline():
    oracle = ScaledServiceEndpointOracle(Mock())
    assert oracle.evaluate()["reason"] == "baseline_not_captured"


def test_scaled_oracle_rejects_replaced_volumes_even_with_healthy_endpoints(monkeypatch):
    monkeypatch.setattr(ServiceEndpointMitigationOracle, "evaluate", lambda self: {"success": True})
    oracle = ScaledServiceEndpointOracle(Mock())
    oracle.canary = "before"
    oracle.pvc_uids = {"data-user-0": "original"}
    oracle._pvc_snapshot = lambda: {"data-user-0": "replacement"}
    oracle.deployment_oracle.evaluate = lambda: {"success": True}
    assert oracle.evaluate()["reason"] == "persistent_volumes_replaced_or_missing"


def test_scaled_oracle_requires_every_replica_to_have_old_and_new_writes(monkeypatch):
    monkeypatch.setattr(ServiceEndpointMitigationOracle, "evaluate", lambda self: {"success": True})
    app = Mock(databases=["users"], scale=SimpleNamespace(members=3), scale_tier="replicated")
    app.command.return_value = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "users", "generation": 1},
                    "spec": {"replicas": 3},
                    "status": {"readyReplicas": 3, "observedGeneration": 1},
                }
            ]
        }
    )
    oracle = ScaledServiceEndpointOracle(SimpleNamespace(app=app))
    oracle.canary = "before"
    oracle._pvc_snapshot = lambda: {}
    oracle.deployment_oracle.evaluate = lambda: {"success": True}
    app.mongo.side_effect = ["", "", "", RuntimeError("secondary lost its data")]
    assert not oracle.evaluate()["success"]
    app.check_workflow.assert_not_called()


def test_scaled_oracle_rejects_scale_down_before_old_members_terminate(monkeypatch):
    monkeypatch.setattr(ServiceEndpointMitigationOracle, "evaluate", lambda self: {"success": True})
    app = Mock(databases=["users"], scale=SimpleNamespace(members=3))
    app.command.return_value = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "users", "generation": 2},
                    "spec": {"replicas": 0},
                    "status": {"readyReplicas": 3, "observedGeneration": 1},
                }
            ]
        }
    )
    oracle = ScaledServiceEndpointOracle(SimpleNamespace(app=app))
    oracle.canary = "before"
    oracle._pvc_snapshot = lambda: {}
    oracle.deployment_oracle.evaluate = lambda: {"success": True}
    assert oracle.evaluate()["reason"] == "database_members_not_ready"
    app.mongo.assert_not_called()
