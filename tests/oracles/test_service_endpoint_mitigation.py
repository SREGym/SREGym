from types import SimpleNamespace

from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle


def _deployment(name, replicas=1, ready=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, generation=1),
        spec=SimpleNamespace(replicas=replicas, selector=SimpleNamespace(match_labels={"service": name})),
        status=SimpleNamespace(
            observed_generation=1,
            updated_replicas=ready,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=0 if ready == replicas else replicas - ready,
        ),
    )


def _replica_set(name, replicas=1):
    return SimpleNamespace(metadata=SimpleNamespace(name=name), spec=SimpleNamespace(replicas=replicas))


def _pod(name, service, owner):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={"service": service},
            owner_references=[SimpleNamespace(kind="ReplicaSet", name=owner)],
            deletion_timestamp=None,
        )
    )


def _address(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _CoreV1:
    def __init__(self, addresses, probe_phase="Succeeded", probe_logs="SERVICE_OK\n"):
        self.endpoints = SimpleNamespace(subsets=[SimpleNamespace(addresses=addresses)])
        self.probe_phase = probe_phase
        self.probe_logs = probe_logs
        self.endpoint_requests = []
        self.created = []
        self.deleted = []

    def read_namespaced_endpoints(self, name, namespace):
        self.endpoint_requests.append((name, namespace))
        return self.endpoints

    def create_namespaced_pod(self, namespace, body):
        self.created.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.probe_phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.probe_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, pods, addresses, deployment=None, replica_sets=None, **probe):
        self.pods = pods
        self.deployment = deployment or _deployment("user-service")
        self.replica_sets = replica_sets or [_replica_set("user-service-rs")]
        self.core_v1_api = _CoreV1(addresses, **probe)

    def get_deployment(self, name, namespace):
        return self.deployment

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)

    def get_matching_replicasets(self, namespace, deployment_name):
        return self.replica_sets


def _evaluate(kubectl):
    problem = SimpleNamespace(
        namespace="social-network",
        faulty_service="user-service",
        expected_service_port=9090,
        kubectl=kubectl,
    )
    oracle = ServiceEndpointMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle.evaluate()["success"]


def test_accepts_current_ready_reachable_endpoint():
    kubectl = _KubeCtl(
        [_pod("user-service-abc", "user-service", "user-service-rs")],
        [_address("user-service-abc")],
    )

    assert _evaluate(kubectl) is True
    command = kubectl.core_v1_api.created[0][1].spec.containers[0].command[-1]
    assert "user-service.social-network.svc.cluster.local' 9090" in command
    assert kubectl.core_v1_api.endpoint_requests == [("user-service", "social-network")]


def test_rejects_empty_ready_endpoints():
    kubectl = _KubeCtl(
        [_pod("user-service-abc", "user-service", "user-service-rs")],
        [],
    )
    assert _evaluate(kubectl) is False
    assert kubectl.core_v1_api.created == []


def test_rejects_endpoint_backed_by_wrong_workload_even_if_relabelled():
    kubectl = _KubeCtl(
        [_pod("compose-post-xyz", "user-service", "compose-post-rs")],
        [_address("compose-post-xyz")],
    )
    assert _evaluate(kubectl) is False
    assert kubectl.core_v1_api.created == []


def test_rejects_endpoint_from_inactive_replicaset():
    kubectl = _KubeCtl(
        [_pod("user-service-old", "user-service", "user-service-old-rs")],
        [_address("user-service-old")],
        replica_sets=[
            _replica_set("user-service-rs"),
            _replica_set("user-service-old-rs", replicas=0),
        ],
    )
    assert _evaluate(kubectl) is False


def test_rejects_scaled_down_deployment():
    kubectl = _KubeCtl([], [], deployment=_deployment("user-service", replicas=0, ready=0))
    assert _evaluate(kubectl) is False


def test_rejects_service_that_has_endpoints_but_cannot_accept_traffic():
    kubectl = _KubeCtl(
        [_pod("user-service-abc", "user-service", "user-service-rs")],
        [_address("user-service-abc")],
        probe_phase="Failed",
        probe_logs="",
    )
    assert _evaluate(kubectl) is False
    assert len(kubectl.core_v1_api.created) == 1
