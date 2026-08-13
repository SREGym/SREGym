from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle


def _service(name, selector=None, port=9090):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(
            selector=selector,
            ports=[] if port is None else [SimpleNamespace(port=port)],
        ),
    )


def _deployment(
    name="frontend",
    *,
    generation=1,
    observed_generation=1,
    replicas=1,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
    dns_policy="ClusterFirst",
    dns_config=None,
):
    pod_spec = SimpleNamespace(dns_policy=dns_policy, dns_config=dns_config)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, generation=generation),
        spec=SimpleNamespace(replicas=replicas, template=SimpleNamespace(spec=pod_spec)),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


class _CoreV1:
    def __init__(self, phase="Succeeded", logs="DNS_OK\n"):
        self.phase = phase
        self.logs = logs
        self.created_pods = []
        self.deleted_pods = []

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, services, deployments, core_v1=None):
        self.services = services
        self.deployments = deployments
        self.core_v1_api = core_v1 or _CoreV1()

    def list_services(self, namespace):
        return SimpleNamespace(items=self.services)

    def get_deployment(self, name, namespace):
        if name not in self.deployments:
            raise ApiException(status=404)
        return self.deployments[name]


def _oracle(kubectl, faulty_service="frontend"):
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        faulty_service=faulty_service,
        kubectl=kubectl,
    )
    oracle = DNSResolutionMitigationOracle(problem)
    oracle.poll_interval_seconds = 0
    oracle.rollout_timeout_seconds = 0
    return oracle


def test_checks_only_the_faulty_service_fqdn():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(
        services=[
            _service("frontend", {"app": "frontend"}),
            _service("unrelated-broken-service", {"app": "broken"}),
        ],
        deployments={"frontend": _deployment()},
        core_v1=core_v1,
    )

    assert _oracle(kubectl).evaluate()["success"] is True
    assert len(core_v1.created_pods) == 1
    command = core_v1.created_pods[0][1].spec.containers[0].command[-1]
    assert "frontend.astronomy-shop.svc.cluster.local" in command
    assert "nslookup frontend.astronomy-shop.svc.cluster.local" in command
    assert "nc -z -w 5 frontend.astronomy-shop.svc.cluster.local 9090" in command
    assert "unrelated-broken-service" not in command


def test_probe_inherits_the_affected_deployment_dns_settings():
    dns_config = SimpleNamespace(nameservers=["8.8.8.8"])
    core_v1 = _CoreV1(phase="Failed", logs="server can't find frontend: NXDOMAIN\n")
    kubectl = _KubeCtl(
        services=[_service("frontend", {"app": "frontend"})],
        deployments={"frontend": _deployment(dns_policy="None", dns_config=dns_config)},
        core_v1=core_v1,
    )

    assert _oracle(kubectl).evaluate()["success"] is False
    probe_spec = core_v1.created_pods[0][1].spec
    assert probe_spec.dns_policy == "None"
    assert probe_spec.dns_config is dns_config
    assert len(core_v1.deleted_pods) == 1


def test_rejects_missing_faulty_service():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(services=[], deployments={}, core_v1=core_v1)

    assert _oracle(kubectl).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_unavailable_source_deployment():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(
        services=[_service("frontend", {"app": "frontend"})],
        deployments={"frontend": _deployment(available=0)},
        core_v1=core_v1,
    )

    assert _oracle(kubectl).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_successful_dns_lookup_when_tcp_connection_fails():
    core_v1 = _CoreV1(phase="Failed", logs="")
    kubectl = _KubeCtl(
        services=[_service("frontend", {"app": "frontend"}, port=8080)],
        deployments={"frontend": _deployment()},
        core_v1=core_v1,
    )

    assert _oracle(kubectl).evaluate()["success"] is False
    command = core_v1.created_pods[0][1].spec.containers[0].command[-1]
    assert "nslookup frontend.astronomy-shop.svc.cluster.local" in command
    assert "nc -z -w 5 frontend.astronomy-shop.svc.cluster.local 8080" in command


def test_rejects_service_without_a_usable_port():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(
        services=[_service("frontend", {"app": "frontend"}, port=None)],
        deployments={"frontend": _deployment()},
        core_v1=core_v1,
    )

    assert _oracle(kubectl).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_available_old_replicas_before_current_rollout_completes():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(
        services=[_service("frontend", {"app": "frontend"})],
        deployments={
            "frontend": _deployment(
                generation=2,
                observed_generation=1,
                updated=0,
                ready=1,
                available=1,
                unavailable=1,
            )
        },
        core_v1=core_v1,
    )

    assert _oracle(kubectl).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_cluster_wide_fault_skips_services_without_deployments():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(
        services=[
            _service("external-name", None),
            _service("orphan", {"app": "orphan"}),
            _service("frontend", {"app": "frontend"}),
        ],
        deployments={"frontend": _deployment()},
        core_v1=core_v1,
    )

    assert _oracle(kubectl, faulty_service=None).evaluate()["success"] is True
    command = core_v1.created_pods[0][1].spec.containers[0].command[-1]
    assert "frontend.astronomy-shop.svc.cluster.local" in command
