from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.conductor.oracles import train_ticket
from sregym.conductor.oracles.train_ticket import TrainTicketMitigationOracle
from sregym.conductor.oracles.workload import WorkloadOracle
from sregym.conductor.problems import train_ticket_f22


@pytest.mark.parametrize(
    "phase,owner_kind,ready,success",
    [
        ("Succeeded", "Job", False, True),
        ("Failed", "Job", False, False),
        ("Succeeded", "ReplicaSet", False, False),
        ("Running", "Job", False, False),
        ("Running", "ReplicaSet", True, True),
    ],
)
def test_only_successful_job_pods_are_exempt(phase, owner_kind, ready, success):
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="test-pod",
            owner_references=[
                client.V1OwnerReference(api_version="v1", kind=owner_kind, name="owner", uid="owner-uid")
            ],
        ),
        status=client.V1PodStatus(
            phase=phase,
            container_statuses=[
                client.V1ContainerStatus(
                    name="container",
                    image="image",
                    image_id="image-id",
                    restart_count=0,
                    ready=ready,
                    state=client.V1ContainerState(),
                )
            ],
        ),
    )
    oracle = TrainTicketMitigationOracle(SimpleNamespace())
    assert (oracle.pods_unready([pod]) is None) is success


def test_f22_uses_the_created_workload_and_forwards_baseline(monkeypatch):
    app = SimpleNamespace(namespace="train-ticket")
    app.create_workload = lambda: setattr(app, "wrk", object())
    kubectl = Mock()
    kubectl.list_deployments.return_value.items = [
        SimpleNamespace(metadata=SimpleNamespace(name="ts-contacts-service"), spec=SimpleNamespace(replicas=1))
    ]
    monkeypatch.setattr(train_ticket_f22, "TrainTicket", lambda: app)
    monkeypatch.setattr(train_ticket_f22, "KubeCtl", lambda: kubectl)
    monkeypatch.setattr(train_ticket_f22, "LLMAsAJudgeOracle", Mock())
    problem = train_ticket_f22.TrainTicketF22()
    health, workload = problem.mitigation_oracle.oracles.values()
    assert isinstance(health, TrainTicketMitigationOracle)
    assert isinstance(workload, WorkloadOracle)
    assert workload.wrk is app.wrk
    problem.mitigation_oracle.capture_baseline()
    assert health.replica_count == {"ts-contacts-service": 1}


def test_health_can_settle_within_the_existing_budget(monkeypatch):
    oracle = TrainTicketMitigationOracle(SimpleNamespace(kubectl=Mock(), namespace="train-ticket"))
    oracle.rollout_time = 0.1
    monkeypatch.setattr(train_ticket, "_HEALTH_POLL_SECONDS", 0.001)
    monkeypatch.setattr(
        oracle,
        "_evaluate_current_state",
        Mock(side_effect=[{"success": False}, {"success": True}, {"success": True}]),
    )
    assert oracle.evaluate()["success"] is True
    assert oracle._evaluate_current_state.call_count == 3


def test_settle_timeout_does_not_accept_persistent_failure(monkeypatch):
    oracle = TrainTicketMitigationOracle(SimpleNamespace(kubectl=Mock(), namespace="train-ticket"))
    oracle.rollout_time = 0.01
    monkeypatch.setattr(train_ticket, "_HEALTH_POLL_SECONDS", 0.001)
    monkeypatch.setattr(oracle, "_evaluate_current_state", Mock(return_value={"success": False}))
    assert oracle.evaluate()["success"] is False
    assert oracle._evaluate_current_state.call_count > 1
