from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.generators.fault import inject_app
from sregym.generators.fault.inject_app import ApplicationFaultInjector


def pod(reason="OOMKilled", event="old", container="kafka"):
    state = client.V1ContainerState(
        terminated=client.V1ContainerStateTerminated(exit_code=137, reason=reason, container_id=event)
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(uid="pod"),
        status=SimpleNamespace(container_statuses=[SimpleNamespace(name=container, state=None, last_state=state)]),
    )


@pytest.fixture
def injector(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(inject_app.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(inject_app.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    injector = object.__new__(ApplicationFaultInjector)
    injector.namespace = "test"
    injector.kubectl = Mock()
    container = client.V1Container(
        name="kafka",
        env=[client.V1EnvVar(name="KAFKA_HEAP_OPTS", value="-Xmx400M")],
        resources=client.V1ResourceRequirements(limits={"memory": "600Mi"}),
    )
    kafka = SimpleNamespace(
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
    )
    checkout = SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[]))))
    injector.kubectl.get_deployment.side_effect = [kafka, checkout]
    return injector, clock


@pytest.mark.parametrize("delay", [125, 210, 595, 600])
def test_waits_for_new_oom_past_old_deadline(injector, delay):
    subject, clock = injector
    subject.kubectl.get_deployment_pods.side_effect = lambda *_: [pod(event="new" if clock.now >= delay else "old")]
    assert subject.inject_kafka_producer_leak() == ["-Xmx400M", "600Mi"]
    assert clock.now == delay


@pytest.mark.parametrize("evidence", [[pod()], [pod(reason="Error")], [pod(container="other")], []])
def test_old_oom_or_unrelated_crash_does_not_pass(injector, evidence):
    subject, clock = injector
    subject.kubectl.get_deployment_pods.return_value = evidence
    with pytest.raises(TimeoutError, match="new OOM within 600"):
        subject.inject_kafka_producer_leak()
    assert clock.now == 600


def test_partial_api_failure_is_not_reported_as_success(injector):
    subject, _ = injector
    subject.kubectl.get_deployment_pods.return_value = []
    subject.kubectl.update_deployment.side_effect = [None, client.ApiException(status=500)]
    with pytest.raises(client.ApiException):
        subject.inject_kafka_producer_leak()
