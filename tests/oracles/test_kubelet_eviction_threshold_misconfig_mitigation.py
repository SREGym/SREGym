from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
    KubeletEvictionThresholdMisconfigMitigationOracle,
)


def _deployment(name, replicas=1, node="worker-b"):
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, generation=1),
        spec=client.V1DeploymentSpec(
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(containers=[client.V1Container(name="app")], node_name=node)
            ),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=1,
            replicas=replicas,
            updated_replicas=replicas,
            ready_replicas=replicas,
            available_replicas=replicas,
        ),
    )


@pytest.fixture(params=[("currency", "frontend"), ("svc-notification", "svc-message")])
def oracle(request):
    target, companion = request.param
    deployments = {name: _deployment(name) for name in request.param}
    services = {
        name: client.V1Service(metadata=client.V1ObjectMeta(name=name), spec=client.V1ServiceSpec())
        for name in deployments
    }
    nodes = {"worker-a": True, "worker-b": False}

    def pods(namespace):
        return SimpleNamespace(
            items=[
                client.V1Pod(
                    metadata=client.V1ObjectMeta(name=name),
                    spec=deployment.spec.template.spec,
                    status=client.V1PodStatus(
                        phase="Running",
                        container_statuses=[
                            client.V1ContainerStatus(
                                name="app",
                                ready=True,
                                restart_count=0,
                                image="application",
                                image_id="application",
                                state=client.V1ContainerState(running=client.V1ContainerStateRunning()),
                            )
                        ],
                    ),
                )
                for name, deployment in deployments.items()
                if deployment.spec.replicas
            ]
        )

    core = SimpleNamespace(
        list_namespaced_service=lambda _: SimpleNamespace(items=list(services.values())),
        list_namespaced_persistent_volume_claim=lambda _: SimpleNamespace(items=[]),
    )
    kubectl = SimpleNamespace(
        deployments=deployments,
        services=services,
        nodes=nodes,
        list_deployments=lambda _: SimpleNamespace(items=list(deployments.values())),
        list_pods=pods,
        list_nodes=lambda: SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name=name),
                    status=SimpleNamespace(conditions=[SimpleNamespace(type="DiskPressure", status=str(active))]),
                )
                for name, active in nodes.items()
            ]
        ),
        core_v1_api=core,
        apps_v1_api=SimpleNamespace(list_namespaced_stateful_set=lambda _: SimpleNamespace(items=[])),
        get_node_free_pct=Mock(return_value=80),
    )
    problem = SimpleNamespace(
        namespace="application",
        faulty_service=target,
        companion=companion,
        target_node="worker-a",
        injector=Mock(),
        kubectl=kubectl,
    )
    result = KubeletEvictionThresholdMisconfigMitigationOracle(problem)
    result.capture_baseline()
    result.rollout_time = 0
    result._node_fix_applied = Mock(return_value=True)
    return result


@pytest.mark.parametrize("node_fixed", [True, False])
@pytest.mark.parametrize("target", [True, False])
def test_required_workload_at_zero_cannot_pass_either_repair_path(oracle, node_fixed, target):
    problem = oracle.problem
    name = problem.faulty_service if target else problem.companion
    problem.kubectl.deployments[name] = _deployment(name, replicas=0)
    oracle._node_fix_applied.return_value = node_fixed

    result = oracle.evaluate()

    assert result["reason"] == "required_deployment_scaled_to_zero"
    assert result["detail"]["deployment"] == name
    oracle._node_fix_applied.assert_not_called()


@pytest.mark.parametrize("node_fixed", [True, False])
def test_deleted_target_cannot_pass_either_repair_path(oracle, node_fixed):
    del oracle.problem.kubectl.deployments[oracle.problem.faulty_service]
    oracle._node_fix_applied.return_value = node_fixed

    assert oracle.evaluate()["reason"] == "required_deployment_missing"
    oracle._node_fix_applied.assert_not_called()


def test_missing_service_cannot_pass_with_a_repaired_node(oracle):
    del oracle.problem.kubectl.services[oracle.problem.faulty_service]

    assert oracle.evaluate()["reason"] == "required_resource_missing"
    oracle._node_fix_applied.assert_not_called()


def test_incomplete_current_rollout_cannot_pass_with_a_repaired_node(oracle):
    oracle.problem.kubectl.deployments[oracle.problem.faulty_service].metadata.generation = 2

    assert oracle.evaluate()["reason"] == "deployment_replicas_unready"
    oracle._node_fix_applied.assert_not_called()


@pytest.mark.parametrize("node_fixed", [True, False])
def test_accepts_both_repair_paths_without_freezing_healthy_replica_counts(oracle, node_fixed):
    target = oracle.problem.faulty_service
    oracle.problem.kubectl.deployments[target] = _deployment(target, replicas=3)
    oracle._node_fix_applied.return_value = node_fixed
    oracle._wait_for_rollouts = Mock(wraps=oracle._wait_for_rollouts)

    assert oracle.evaluate()["success"] is True
    oracle._wait_for_rollouts.assert_called_once()


def test_relocation_cannot_leave_target_pinned_to_disk_pressure(oracle):
    oracle._node_fix_applied.return_value = False
    oracle.problem.kubectl.deployments[oracle.problem.faulty_service].spec.template.spec.node_name = "worker-a"

    assert oracle.evaluate()["reason"] == "fault_still_present"


def test_relocation_accepts_removing_the_bad_pin(oracle):
    oracle._node_fix_applied.return_value = False
    oracle.problem.kubectl.deployments[oracle.problem.faulty_service].spec.template.spec.node_name = None

    assert oracle.evaluate()["success"] is True


def test_application_api_failure_is_not_reported_as_a_successful_node_repair(oracle):
    oracle.problem.kubectl.core_v1_api.list_namespaced_service = Mock(side_effect=ApiException(status=503))

    with pytest.raises(ApiException, match="503"):
        oracle.evaluate()
    oracle._node_fix_applied.assert_not_called()


@pytest.mark.parametrize("node_fixed", [True, False])
@pytest.mark.parametrize(("phase", "reason"), [("Failed", "Evicted"), ("Succeeded", None)])
def test_terminal_eviction_records_do_not_reject_healthy_replacements(oracle, node_fixed, phase, reason):
    original = oracle.problem.kubectl.list_pods
    evicted = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="old-evicted",
            owner_references=[
                client.V1OwnerReference(
                    api_version="apps/v1", kind="ReplicaSet", name="old", uid="old", controller=True
                )
            ],
        ),
        status=client.V1PodStatus(phase=phase, reason=reason),
    )
    oracle.problem.kubectl.list_pods = lambda ns: SimpleNamespace(items=[evicted, *original(ns).items])
    oracle._node_fix_applied.return_value = node_fixed

    assert oracle.evaluate()["success"] is True
    target = oracle.problem.kubectl.deployments[oracle.problem.faulty_service]
    target.status.ready_replicas = 0
    assert oracle.evaluate()["reason"] == "deployment_replicas_unready"


@pytest.mark.parametrize(
    ("phase", "reason", "owner_kind", "controller"),
    [
        ("Failed", "Error", "ReplicaSet", True),
        ("Pending", "Evicted", "ReplicaSet", True),
        ("Failed", "Evicted", "StatefulSet", True),
        ("Failed", "Evicted", "ReplicaSet", False),
        ("Failed", "Evicted", None, None),
    ],
)
def test_other_unhealthy_pods_are_not_ignored(oracle, phase, reason, owner_kind, controller):
    original = oracle.problem.kubectl.list_pods
    owners = (
        [
            client.V1OwnerReference(
                api_version="apps/v1", kind=owner_kind, name="owner", uid="owner", controller=controller
            )
        ]
        if owner_kind
        else []
    )
    unhealthy = client.V1Pod(
        metadata=client.V1ObjectMeta(name="unhealthy", owner_references=owners),
        status=client.V1PodStatus(phase=phase, reason=reason),
    )
    oracle.problem.kubectl.list_pods = lambda ns: SimpleNamespace(items=[*original(ns).items, unhealthy])

    assert oracle.evaluate()["reason"] == "pods_not_ready"


def test_evicted_statistics_collector_is_not_silently_ignored(oracle):
    original = oracle.problem.kubectl.list_pods
    collector = client.V1Pod(
        metadata=client.V1ObjectMeta(name="collector", labels={"app": "locust-fetcher"}),
        status=client.V1PodStatus(phase="Failed", reason="Evicted"),
    )
    oracle.problem.kubectl.list_pods = lambda ns: SimpleNamespace(items=[collector, *original(ns).items])

    assert oracle.evaluate()["reason"] == "pods_not_ready"


@pytest.mark.parametrize("phase", ["Failed", "Pending"])
def test_node_local_daemon_requires_a_healthy_peer_of_the_same_controller(oracle, phase):
    original = oracle.problem.kubectl.list_pods
    owner = client.V1OwnerReference(api_version="apps/v1", kind="DaemonSet", name="agent", uid="agent", controller=True)
    healthy = original("application").items[0]
    healthy.metadata.owner_references = [owner]
    local = client.V1Pod(
        metadata=client.V1ObjectMeta(name="local-agent", owner_references=[owner]),
        spec=client.V1PodSpec(containers=[client.V1Container(name="agent")], node_name="worker-a"),
        status=client.V1PodStatus(phase=phase, reason="Evicted" if phase == "Failed" else None),
    )
    oracle._node_fix_applied.return_value = False
    oracle.problem.kubectl.list_pods = lambda ns: SimpleNamespace(items=[healthy, local, *original(ns).items])

    assert oracle.evaluate()["success"] is True
    healthy.spec.node_name = "worker-a"
    assert oracle.evaluate()["reason"] == "pods_not_ready"
    healthy.spec.node_name = "worker-b"
    healthy.metadata.owner_references = []
    assert oracle.evaluate()["reason"] == "pods_not_ready"
    healthy.metadata.owner_references = [owner]
    local.spec.node_name = "worker-b"
    assert oracle.evaluate()["reason"] == "pods_not_ready"
    local.spec.node_name = "worker-a"
    local.status.phase, local.status.reason = "Failed", "Error"
    assert oracle.evaluate()["reason"] == "pods_not_ready"


@pytest.mark.parametrize(
    ("config", "pressure", "expected"),
    [
        ("", False, True),
        ("", True, False),
        ('nodefs.available: "10%"', False, True),
        ('nodefs.available: "80%"', False, False),
        ('nodefs.available: "90%"', False, False),
        ("nodefs.available: invalid", False, False),
    ],
)
def test_node_repair_still_requires_a_safe_threshold_and_cleared_pressure(oracle, config, pressure, expected):
    del oracle._node_fix_applied
    oracle._read_kubelet_config = Mock(return_value=config)
    oracle.problem.kubectl.nodes["worker-a"] = pressure

    assert oracle._node_fix_applied(oracle.problem.injector, oracle.problem.kubectl, "worker-a") is expected
