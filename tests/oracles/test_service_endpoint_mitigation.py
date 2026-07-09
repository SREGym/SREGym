from types import SimpleNamespace

from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle


def _pod(name, labels):
    return SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels))


def _address(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _KubeCtl:
    def __init__(self, pods, addresses):
        self.pods = pods
        self.endpoints = SimpleNamespace(subsets=[SimpleNamespace(addresses=addresses)])
        self.endpoint_requests = []
        self.core_v1_api = SimpleNamespace(read_namespaced_endpoints=self._read_endpoints)

    def get_deployment(self, name, namespace):
        selector = SimpleNamespace(match_labels={"service": name})
        return SimpleNamespace(spec=SimpleNamespace(selector=selector))

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)

    def _read_endpoints(self, name, namespace):
        self.endpoint_requests.append((name, namespace))
        return self.endpoints


def _evaluate(kubectl):
    problem = SimpleNamespace(
        namespace="social-network",
        faulty_service="user-service",
        kubectl=kubectl,
    )
    return ServiceEndpointMitigationOracle(problem).evaluate()["success"]


def test_accepts_ready_endpoints_for_the_affected_deployment():
    kubectl = _KubeCtl(
        pods=[
            _pod("user-service-abc", {"service": "user-service"}),
            _pod("unrelated-pending", {"service": "unrelated"}),
        ],
        addresses=[_address("user-service-abc")],
    )

    assert _evaluate(kubectl) is True
    assert kubectl.endpoint_requests == [("user-service", "social-network")]


def test_rejects_empty_ready_endpoints():
    kubectl = _KubeCtl(
        pods=[_pod("user-service-abc", {"service": "user-service"})],
        addresses=[],
    )

    assert _evaluate(kubectl) is False


def test_rejects_endpoints_backed_by_the_wrong_workload():
    kubectl = _KubeCtl(
        pods=[
            _pod("user-service-abc", {"service": "user-service"}),
            _pod("compose-post-service-xyz", {"service": "compose-post-service"}),
        ],
        addresses=[_address("compose-post-service-xyz")],
    )

    assert _evaluate(kubectl) is False


def test_rejects_when_the_intended_deployment_has_no_pods():
    kubectl = _KubeCtl(
        pods=[_pod("unrelated-abc", {"service": "unrelated"})],
        addresses=[_address("unrelated-abc")],
    )

    assert _evaluate(kubectl) is False
    assert kubectl.endpoint_requests == []
