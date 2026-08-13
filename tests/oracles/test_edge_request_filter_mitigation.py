from types import SimpleNamespace

from sregym.conductor.oracles.edge_request_filter_mitigation import (
    EdgeRequestFilterMitigationOracle,
)

BAD_REGEX = "^([a-zA-Z]+)*$"
SAFE_REGEX = "^[A-Za-z]+$"
INJECTED_SCRIPT = f"my $rule = $ENV{{WAF_RULE_REGEX}} || q{{{BAD_REGEX}}};"


def _container(*, regex=None, enabled=None, command=None, args=None):
    env = []
    if regex is not None:
        env.append(SimpleNamespace(name="WAF_RULE_REGEX", value=regex))
    if enabled is not None:
        env.append(SimpleNamespace(name="WAF_RULE_ENABLED", value=enabled))
    return SimpleNamespace(
        name="frontend-proxy",
        env=env,
        command=["/usr/bin/perl", "-e"] if command is None else command,
        args=[INJECTED_SCRIPT] if args is None else args,
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
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=[container or _container(regex=SAFE_REGEX, enabled="true")])
            ),
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
    def __init__(
        self,
        *,
        endpoint_pods=None,
        probe_phase="Succeeded",
        probe_logs="CRAFTED_OK\nNORMAL_OK\n",
    ):
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
        regex_env="WAF_RULE_REGEX",
        bad_regex=BAD_REGEX,
        crafted_payload="a" * 5000 + "!",
        kubectl=kubectl,
    )
    oracle = EdgeRequestFilterMitigationOracle(problem)
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_safe_regex_after_runtime_probes_succeed():
    core_v1 = _CoreV1()
    oracle = _oracle(_KubeCtl(core_v1=core_v1))

    assert oracle.evaluate()["success"] is True
    assert len(core_v1.created_pods) == 1
    assert len(core_v1.deleted_pods) == 1
    script = core_v1.created_pods[0][1].spec.containers[0].command[-1]
    assert "-T 3 -t 1" in script
    assert "a" * 5000 + "!" in script
    assert "/tmp/normal" in script


def test_accepts_exactly_disabled_bad_rule():
    deployment = _deployment(container=_container(regex=BAD_REGEX, enabled="false"))

    assert _oracle(_KubeCtl(deployment=deployment)).evaluate()["success"] is True


def test_accepts_full_rollback_without_waf_environment():
    original = _container(command=["/docker-entrypoint.sh"], args=["envoy", "-c", "/etc/envoy.yaml"])
    deployment = _deployment(container=original)

    assert _oracle(_KubeCtl(deployment=deployment)).evaluate()["success"] is True


def test_rejects_exact_bad_rule_without_starting_probe():
    core_v1 = _CoreV1()
    deployment = _deployment(container=_container(regex=BAD_REGEX, enabled="true"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_missing_regex_when_injected_script_uses_bad_fallback():
    core_v1 = _CoreV1()
    deployment = _deployment(container=_container(enabled="true"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_rejects_uppercase_false_because_injected_script_keeps_rule_enabled():
    core_v1 = _CoreV1()
    deployment = _deployment(container=_container(regex=BAD_REGEX, enabled="FALSE"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created_pods == []


def test_runtime_probe_rejects_an_alternate_vulnerable_regex():
    core_v1 = _CoreV1(probe_phase="Failed", probe_logs="")
    deployment = _deployment(container=_container(regex="^([a-z]+)+$", enabled="true"))

    assert _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.deleted_pods) == 1


def test_rejects_probe_that_does_not_complete_normal_request():
    core_v1 = _CoreV1(probe_logs="CRAFTED_OK\n")

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
