from types import SimpleNamespace

from kubernetes import client as kube_client

from sregym.conductor.oracles.endpointslice_stale_after_rollout_mitigation import (
    EndpointSliceStaleAfterRolloutMitigationOracle,
)
from sregym.conductor.problems.registry import ProblemRegistry


class FakeContainerState:
    def __init__(self):
        self.waiting = None
        self.terminated = None


class FakeContainerStatus:
    def __init__(self, ready=True):
        self.name = "probe"
        self.ready = ready
        self.state = FakeContainerState()


class FakePodStatus:
    def __init__(self, phase: str, pod_ip: str):
        self.phase = phase
        self.pod_ip = pod_ip
        self.container_statuses = [FakeContainerStatus(ready=True)]


class FakePodMetadata:
    def __init__(self, labels: dict[str, str]):
        self.labels = labels


class FakePod:
    def __init__(self, name: str, labels: dict[str, str], pod_ip: str):
        self.metadata = FakePodMetadata(labels)
        self.status = FakePodStatus(phase="Running", pod_ip=pod_ip)


class FakePodList:
    def __init__(self, items):
        self.items = items


class FakeDiscoveryV1Api:
    def __init__(self, slices):
        self._slices = slices

    def list_namespaced_endpoint_slice(self, namespace, label_selector=None):
        return SimpleNamespace(items=self._slices)


class FakeKubeCtl:
    def __init__(self, pods, selector=None):
        self._pods = pods
        self._selector = selector or {"io.kompose.service": "frontend"}

    def list_pods(self, namespace):
        return FakePodList(self._pods)

    def list_deployments(self, namespace):
        return SimpleNamespace(items=[])

    def get_service_json(self, service_name, namespace):
        return {"spec": {"selector": self._selector}}


def test_endpointslice_problem_registered():
    registry = ProblemRegistry()
    assert "endpointslice_stale_after_rollout_hotel_reservation" in registry.PROBLEM_REGISTRY
    cls = registry.get_problem("endpointslice_stale_after_rollout_hotel_reservation")
    assert cls.__name__ == "EndpointSliceStaleAfterRolloutHotelReservation"


def test_endpointslice_oracle_detects_stale_ips(monkeypatch):
    pods = [FakePod("frontend-1", {"io.kompose.service": "frontend"}, "10.0.0.2")]
    problem = SimpleNamespace(namespace="default", faulty_service="frontend", kubectl=FakeKubeCtl(pods))
    oracle = EndpointSliceStaleAfterRolloutMitigationOracle(problem)
    monkeypatch.setattr(
        kube_client,
        "DiscoveryV1Api",
        lambda: FakeDiscoveryV1Api(
            [
                SimpleNamespace(
                    endpoints=[SimpleNamespace(addresses=["10.0.0.1"]), SimpleNamespace(addresses=["10.0.0.2"])]
                )
            ]
        ),
    )
    monkeypatch.setattr(oracle, "_probe_service", lambda namespace, service: True)

    result = oracle.evaluate()

    assert result["success"] is False
    assert result["stale_endpoint_ips"] == ["10.0.0.1"]


def test_endpointslice_oracle_accepts_exact_match(monkeypatch):
    pods = [FakePod("frontend-1", {"io.kompose.service": "frontend"}, "10.0.0.2")]
    problem = SimpleNamespace(namespace="default", faulty_service="frontend", kubectl=FakeKubeCtl(pods))
    oracle = EndpointSliceStaleAfterRolloutMitigationOracle(problem)
    monkeypatch.setattr(
        kube_client,
        "DiscoveryV1Api",
        lambda: FakeDiscoveryV1Api([SimpleNamespace(endpoints=[SimpleNamespace(addresses=["10.0.0.2"])])]),
    )
    monkeypatch.setattr(oracle, "_probe_service", lambda namespace, service: True)

    result = oracle.evaluate()

    assert result["success"] is True


def test_endpointslice_oracle_detects_missing_ips(monkeypatch):
    pods = [FakePod("frontend-1", {"io.kompose.service": "frontend"}, "10.0.0.2")]
    problem = SimpleNamespace(namespace="default", faulty_service="frontend", kubectl=FakeKubeCtl(pods))
    oracle = EndpointSliceStaleAfterRolloutMitigationOracle(problem)
    monkeypatch.setattr(
        kube_client,
        "DiscoveryV1Api",
        lambda: FakeDiscoveryV1Api([SimpleNamespace(endpoints=[SimpleNamespace(addresses=["10.0.0.3"])])]),
    )
    monkeypatch.setattr(oracle, "_probe_service", lambda namespace, service: True)

    result = oracle.evaluate()

    assert result["success"] is False
    assert result["missing_endpoint_ips"] == ["10.0.0.2"]
