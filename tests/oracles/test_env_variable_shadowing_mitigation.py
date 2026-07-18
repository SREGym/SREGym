from types import SimpleNamespace

from sregym.conductor.oracles.env_variable_shadowing_mitigation import (
    EnvVariableShadowingMitigationOracle,
)


def _env(value):
    return SimpleNamespace(name="FRONTEND_HOST", value=value)


def _container(*host_values):
    return SimpleNamespace(
        name="frontend-proxy",
        env=[_env(value) for value in host_values],
    )


def _deployment(
    *,
    container=None,
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
        metadata=SimpleNamespace(name="frontend-proxy", generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels={"app.kubernetes.io/name": "frontend-proxy"}),
            template=SimpleNamespace(spec=SimpleNamespace(containers=[container or _container("frontend")])),
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


def _pod(name, app="frontend-proxy"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"app.kubernetes.io/name": app}),
    )


def _endpoint(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _CoreV1:
    def __init__(self, *, endpoint_pods=None, probe_phase="Succeeded", probe_logs="FRONTEND_OK\n"):
        endpoint_pods = ["frontend-proxy-abc"] if endpoint_pods is None else endpoint_pods
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
        return SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(port=8080)]))

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.probe_phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.probe_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, *, deployment=None, pods=None, core_v1=None):
        self.deployment = deployment or _deployment()
        self.pods = [_pod("frontend-proxy-abc")] if pods is None else pods
        self.core_v1_api = core_v1 or _CoreV1()

    def get_deployment(self, name, namespace):
        return self.deployment

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)


def _oracle(kubectl):
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        faulty_service="frontend-proxy",
        ENV_NAME="FRONTEND_HOST",
        SHADOW_VALUE="localhost",
        kubectl=kubectl,
    )
    oracle = EnvVariableShadowingMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_single_correct_definition_and_expected_frontend_content():
    core_v1 = _CoreV1()
    oracle = _oracle(_KubeCtl(core_v1=core_v1))

    assert oracle.evaluate()["success"] is True
    assert len(core_v1.created_pods) == 1
    assert len(core_v1.deleted_pods) == 1
    script = core_v1.created_pods[0][1].spec.containers[0].command[-1]
    assert "-T 3 -t 1" in script
    assert "Otel Demo - Home" in script


def test_accepts_equivalent_fqdn_when_frontend_probe_succeeds():
    deployment = _deployment(container=_container("frontend.astronomy-shop.svc.cluster.local"))

    assert _oracle(_KubeCtl(deployment=deployment)).evaluate()["success"] is True


def test_accepts_no_environment_definition_when_equivalent_runtime_config_works():
    deployment = _deployment(container=_container())

    assert _oracle(_KubeCtl(deployment=deployment)).evaluate()["success"] is True


def test_rejects_shadowing_duplicate_without_starting_probe():
    core_v1 = _CoreV1()
    deployment = _deployment(container=_container("frontend", "localhost"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_duplicate_even_when_both_values_are_functional():
    core_v1 = _CoreV1()
    deployment = _deployment(container=_container("frontend", "frontend"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_single_known_bad_value_without_starting_probe():
    core_v1 = _CoreV1()
    deployment = _deployment(container=_container("localhost"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_runtime_probe_rejects_another_broken_upstream_value():
    core_v1 = _CoreV1(probe_phase="Failed", probe_logs="")
    deployment = _deployment(container=_container("127.0.0.1"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1


def test_rejects_successful_probe_without_expected_content_marker():
    core_v1 = _CoreV1(probe_logs="")

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False


def test_rejects_scaled_to_zero_without_waiting_or_probing():
    core_v1 = _CoreV1()
    deployment = _deployment(
        replicas=0,
        current_replicas=0,
        updated=0,
        ready=0,
        available=0,
    )

    assert _oracle(_KubeCtl(deployment=deployment, pods=[], core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_available_old_replica_before_current_rollout_completes():
    core_v1 = _CoreV1()
    deployment = _deployment(
        generation=2,
        observed_generation=1,
        updated=0,
        ready=1,
        available=1,
        unavailable=1,
    )

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_service_endpoint_backed_by_another_workload():
    core_v1 = _CoreV1(endpoint_pods=["frontend-abc"])
    kubectl = _KubeCtl(
        pods=[_pod("frontend-proxy-abc"), _pod("frontend-abc", app="frontend")],
        core_v1=core_v1,
    )

    assert _oracle(kubectl).evaluate()["success"] is False
    assert core_v1.created_pods == []
