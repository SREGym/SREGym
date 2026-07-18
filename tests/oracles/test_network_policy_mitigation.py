from types import SimpleNamespace

from sregym.conductor.oracles.network_policy_oracle import NetworkPolicyMitigationOracle


def _deployment(
    *,
    replicas=1,
    generation=1,
    observed_generation=1,
    current_replicas=1,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="recommendation", generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels={"io.kompose.service": "recommendation"}),
        ),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            replicas=current_replicas,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


def _pod(name, service="recommendation"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"io.kompose.service": service}),
    )


def _endpoint(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _CoreV1:
    def __init__(self, *, endpoint_pods=None, probe_phase="Succeeded", probe_logs="RECOMMENDATION_OK\n"):
        endpoint_pods = ["recommendation-abc"] if endpoint_pods is None else endpoint_pods
        self.endpoints = SimpleNamespace(
            subsets=[SimpleNamespace(addresses=[_endpoint(name) for name in endpoint_pods])]
        )
        self.probe_phase = probe_phase
        self.probe_logs = probe_logs
        self.created_pods = []
        self.deleted_pods = []

    def read_namespaced_endpoints(self, name, namespace):
        return self.endpoints

    def read_namespaced_service(self, name, namespace):
        if name == "recommendation":
            return SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(port=8085)], selector={"service": name}))
        return SimpleNamespace(
            spec=SimpleNamespace(
                ports=[SimpleNamespace(port=5000)],
                selector={"io.kompose.service": "frontend"},
            )
        )

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.probe_phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.probe_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, deployment=None, pods=None, core_v1=None):
        self.deployment = deployment or _deployment()
        self.pods = [_pod("recommendation-abc")] if pods is None else pods
        self.core_v1_api = core_v1 or _CoreV1()

    def get_deployment(self, name, namespace):
        return self.deployment

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)


def _oracle(kubectl):
    problem = SimpleNamespace(
        namespace="hotel-reservation",
        faulty_service="recommendation",
        kubectl=kubectl,
        app=SimpleNamespace(frontend_service="frontend", frontend_port=5000),
    )
    oracle = NetworkPolicyMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_healthy_service_independent_of_network_policy_object_name():
    core_v1 = _CoreV1()
    oracle = _oracle(_KubeCtl(core_v1=core_v1))

    assert oracle.evaluate()["success"] is True
    assert len(core_v1.created_pods) == 1
    assert len(core_v1.deleted_pods) == 1
    script = core_v1.created_pods[0][1].spec.containers[0].command[-1]
    probe = core_v1.created_pods[0][1]
    assert "nc -z -w 5 'recommendation.hotel-reservation.svc.cluster.local' 8085" in script
    assert "/recommendations?require=rate" in script
    assert '"type":"FeatureCollection"' in script
    assert probe.metadata.labels["io.kompose.service"] == "frontend"
    assert probe.spec.containers[0].readiness_probe._exec.command[-1] == "exit 1"


def test_rejects_ongoing_outage_even_if_injected_policy_was_removed():
    core_v1 = _CoreV1(probe_phase="Failed", probe_logs="wget: download timed out\n")
    oracle = _oracle(_KubeCtl(core_v1=core_v1))

    assert oracle.evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1


def test_rejects_scaled_to_zero_without_waiting_or_probing():
    core_v1 = _CoreV1()
    oracle = _oracle(
        _KubeCtl(
            deployment=_deployment(
                replicas=0,
                current_replicas=0,
                updated=0,
                ready=0,
                available=0,
            ),
            pods=[],
            core_v1=core_v1,
        )
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_available_old_replica_before_current_rollout_completes():
    core_v1 = _CoreV1()
    oracle = _oracle(
        _KubeCtl(
            deployment=_deployment(
                generation=2,
                observed_generation=1,
                updated=0,
                ready=1,
                available=1,
                unavailable=1,
            ),
            core_v1=core_v1,
        )
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_service_endpoint_backed_by_another_workload():
    core_v1 = _CoreV1(endpoint_pods=["search-abc"])
    oracle = _oracle(
        _KubeCtl(
            pods=[_pod("recommendation-abc"), _pod("search-abc", service="search")],
            core_v1=core_v1,
        )
    )

    assert oracle.evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_empty_ready_endpoints():
    core_v1 = _CoreV1(endpoint_pods=[])
    oracle = _oracle(_KubeCtl(core_v1=core_v1))

    assert oracle.evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_probe_without_expected_application_response():
    core_v1 = _CoreV1(probe_logs='{"status":"ok"}\n')
    oracle = _oracle(_KubeCtl(core_v1=core_v1))

    assert oracle.evaluate()["success"] is False
