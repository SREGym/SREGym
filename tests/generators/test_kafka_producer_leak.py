import base64
import copy
import datetime
import re
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from kubernetes import client

from sregym.conductor.oracles import kafka_producer_leak_mitigation as grading
from sregym.generators.fault import inject_app
from sregym.service import kafka_health


def deployment(name):
    container = client.V1Container(
        name=name,
        image="kafka-image:current",
        env=[client.V1EnvVar(name="KAFKA_HEAP_OPTS", value="-Xmx400M -Xms400M")],
        resources=client.V1ResourceRequirements(limits={"memory": "1Gi"}),
    )
    return NS(
        spec=NS(
            replicas=1,
            selector=NS(match_labels={"app.kubernetes.io/name": name}),
            template=NS(metadata=NS(annotations=None), spec=NS(containers=[container])),
        )
    )


@pytest.fixture
def clock(monkeypatch):
    state = NS(now=0)
    monkeypatch.setattr(inject_app.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(inject_app.time, "sleep", lambda seconds: setattr(state, "now", state.now + seconds))
    return state


def kube():
    kubectl = MagicMock()
    resources = {name: deployment(name) for name in ("checkout", "kafka")}
    kubectl.get_deployment.side_effect = lambda name, namespace: copy.deepcopy(resources[name])
    kubectl.update_deployment.side_effect = lambda name, namespace, value: resources.__setitem__(
        name, copy.deepcopy(value)
    )
    kubectl.list_pods.return_value = NS(items=[])
    return kubectl, resources


def test_injection_requires_memory_failure_and_failed_delivery(monkeypatch, clock):
    kubectl, resources = kube()
    injector = object.__new__(inject_app.ApplicationFaultInjector)
    injector.namespace, injector.kubectl = "shop", kubectl
    monkeypatch.setattr(inject_app, "broker_memory_failure", lambda *args: True)
    probe = MagicMock()
    probe.check.side_effect = [True, False]
    assert injector._inject_kafka_producers("checkout", probe) == ["-Xmx400M -Xms400M", "1Gi"]
    assert clock.now == 5
    sidecar = resources["checkout"].spec.template.spec.containers[-1]
    encoded = re.search(r"b64decode\('([^']+)'\)", sidecar.command[-1]).group(1)
    source = base64.b64decode(encoded).decode()
    assert "15728640" in source and "range(20)" in source and "producer.flush(10)" in source
    assert "producer.produce('order-events', payload)" in source
    assert "producer.produce('orders'," not in source
    compile(source, "producer", "exec")


def test_memory_errors_without_serving_failure_do_not_complete_injection(monkeypatch, clock):
    kubectl, _ = kube()
    injector = object.__new__(inject_app.ApplicationFaultInjector)
    injector.namespace, injector.kubectl = "shop", kubectl
    monkeypatch.setattr(inject_app, "broker_memory_failure", lambda *args: True)
    with pytest.raises(TimeoutError, match="serving failure"):
        injector._inject_kafka_producers("checkout", NS(check=lambda: True))
    assert clock.now == 300


@pytest.mark.parametrize("rollout_error", [False, True])
def test_recovery_stops_producers_before_one_broker_rollout_and_restores_replicas(clock, rollout_error):
    kubectl, resources = kube()
    injector = object.__new__(inject_app.ApplicationFaultInjector)
    injector.namespace, injector.kubectl = "shop", kubectl
    resources["checkout"].spec.replicas = 2
    resources["checkout"].spec.template.spec.containers.append(client.V1Container(name="order-creator"))
    events = []
    update = kubectl.update_deployment.side_effect

    def record(name, namespace, value):
        events.append((name, value.spec.replicas))
        update(name, namespace, value)

    kubectl.update_deployment.side_effect = record
    old_pod = NS(
        metadata=NS(labels={"app.kubernetes.io/name": "checkout"}),
        spec=NS(containers=[client.V1Container(name="order-creator")]),
    )
    kubectl.list_pods.side_effect = [NS(items=[old_pod]), NS(items=[])]
    if rollout_error:
        kubectl.exec_command_checked.side_effect = RuntimeError("rollout failed")
        with pytest.raises(RuntimeError, match="rollout failed"):
            injector.recover_kafka_producer_leak()
    else:
        injector.recover_kafka_producer_leak()
    assert events == [("checkout", 0), ("kafka", 1), ("checkout", 2)]
    assert clock.now == 2
    assert "kubectl.kubernetes.io/restartedAt" in resources["kafka"].spec.template.metadata.annotations
    assert all(c.name != "order-creator" for c in resources["checkout"].spec.template.spec.containers)
    assert len(kubectl.exec_command_checked.call_args_list) == 1


@pytest.mark.parametrize("fresh,reason", [(True, "Error"), (False, "Error"), (True, "OOMKilled")])
def test_fresh_previous_container_memory_failure(monkeypatch, fresh, reason):
    now = datetime.datetime.now(datetime.UTC)
    finished = now + datetime.timedelta(seconds=1 if fresh else -1)
    terminated = NS(reason=reason, finished_at=finished)
    status = NS(name="kafka", state=NS(running=None, terminated=None), last_state=NS(terminated=terminated))
    pod = NS(metadata=NS(name="kafka-1"), status=NS(container_statuses=[status]))
    monkeypatch.setattr(kafka_health, "broker_pods", lambda *args: [pod])
    kubectl = MagicMock()
    kubectl.exec_command_checked.return_value = "java.lang.OutOfMemoryError: Java heap space"
    assert kafka_health.broker_memory_failure(kubectl, "shop", now) is fresh
    if fresh and reason == "Error":
        assert "--previous" in kubectl.exec_command_checked.call_args.args[0]
    else:
        kubectl.exec_command_checked.assert_not_called()


def oracle(monkeypatch):
    kubectl, resources = kube()
    problem = NS(
        kubectl=kubectl, namespace="shop", faulty_service="checkout", heap_limit="-Xmx400M -Xms400M", memory_limit="1Gi"
    )
    evaluator = grading.KafkaProducerLeakOracle(problem)
    monkeypatch.setattr(grading.MitigationOracle, "evaluate", lambda self: {"success": True})
    monkeypatch.setattr(evaluator, "_broker_restarts", lambda: {"uid-1": 0})
    monkeypatch.setattr(grading, "broker_memory_failure", lambda *args: False)
    probe = MagicMock()
    probe.__enter__.return_value = probe
    monkeypatch.setattr(grading, "KafkaHealthCheck", lambda *args: probe)
    return evaluator, probe, resources


def test_oracle_allows_startup_then_observes_full_window(monkeypatch, clock):
    evaluator, probe, _ = oracle(monkeypatch)

    def check():
        return clock.now >= 10

    probe.check.side_effect = check
    assert evaluator.evaluate()["success"] is True
    assert clock.now >= 130


def test_oracle_rejects_running_broker_with_fresh_heap_failure(monkeypatch, clock):
    evaluator, _, _ = oracle(monkeypatch)
    monkeypatch.setattr(grading, "broker_memory_failure", lambda *args: True)
    assert evaluator.evaluate()["reason"] == "fault_still_present"


def test_oracle_rejects_unavailable_broker_after_startup_grace(monkeypatch, clock):
    evaluator, probe, _ = oracle(monkeypatch)
    probe.check.return_value = False
    assert evaluator.evaluate()["reason"] == "kafka_not_serving"
    assert clock.now == 60


def test_oracle_rejects_removed_heap_limit(monkeypatch, clock):
    evaluator, _, resources = oracle(monkeypatch)
    resources["kafka"].spec.template.spec.containers[0].env = []
    assert evaluator.evaluate()["reason"] == "fault_still_present"


def test_client_pod_uses_same_image_separate_memory_and_is_deleted():
    kubectl, _ = kube()
    with kafka_health.KafkaHealthCheck(kubectl, "shop"):
        pod = kubectl.core_v1_api.create_namespaced_pod.call_args.args[1]
        assert pod.spec.containers[0].image == "kafka-image:current"
        assert pod.spec.containers[0].resources.limits["memory"] == "256Mi"
        assert pod.spec.automount_service_account_token is False
    kubectl.core_v1_api.delete_namespaced_pod.assert_called_once()


def test_client_pod_startup_error_still_deletes_pod():
    kubectl, _ = kube()
    kubectl.exec_command_checked.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"), kafka_health.KafkaHealthCheck(kubectl, "shop"):
        pass
    kubectl.core_v1_api.delete_namespaced_pod.assert_called_once()
