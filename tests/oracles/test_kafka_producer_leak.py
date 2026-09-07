import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from sregym.conductor.oracles import kafka_producer_leak_mitigation as module
from sregym.conductor.oracles.mitigation import MitigationOracle


class Clock:
    def __init__(self):
        self.now = 0
        self.on_sleep = lambda: None

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


def workload(name):
    container = client.V1Container(
        name=name,
        env=[client.V1EnvVar(name="KAFKA_HEAP_OPTS", value="-Xmx400M -Xms400M")],
        resources=client.V1ResourceRequirements(limits={"memory": "600Mi"}),
    )
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, uid=name + "-deployment", generation=1),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=[container])),
        ),
        status=client.V1DeploymentStatus(
            observed_generation=1, replicas=1, updated_replicas=1, ready_replicas=1, available_replicas=1
        ),
    )
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=name + "-pod", uid=name + "-pod-uid"),
        spec=client.V1PodSpec(containers=[copy.deepcopy(container)]),
        status=client.V1PodStatus(
            phase="Running",
            container_statuses=[
                client.V1ContainerStatus(
                    name=name,
                    ready=True,
                    restart_count=0,
                    image="test",
                    image_id="test",
                    container_id=name + "-process",
                    state=client.V1ContainerState(running=client.V1ContainerStateRunning()),
                )
            ],
        ),
    )
    return deployment, pod


@pytest.fixture
def case(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(MitigationOracle, "_wait_for_rollouts", lambda *args: None)
    monkeypatch.setattr(MitigationOracle, "_evaluate_current_state", lambda *args: {"success": True})
    resources = {name: workload(name) for name in ("checkout", "kafka")}
    kubectl = Mock()
    kubectl.get_deployment.side_effect = lambda name, namespace: resources[name][0]
    kubectl.list_deployments.return_value = SimpleNamespace(items=[v[0] for v in resources.values()])
    kubectl.get_deployment_pods.side_effect = lambda deployment, namespace: [resources[deployment.metadata.name][1]]
    problem = SimpleNamespace(kubectl=kubectl, namespace="test", faulty_service="checkout")
    oracle = module.KafkaProducerLeakOracle(problem)
    oracle.capture_baseline()
    return oracle, resources, clock


def test_stable_recovery_with_old_restarts_waits_full_window(case):
    oracle, resources, clock = case
    resources["kafka"][1].status.container_statuses[0].restart_count = 3
    assert oracle.evaluate() == {"success": True, "stable_seconds": 600}
    assert clock.now == 600


def test_retained_inert_sidecar_is_a_valid_fix(case):
    oracle, resources, clock = case
    dep, pod = resources["checkout"]
    container = client.V1Container(name="order-creator", command=["sleep", "3600"])
    dep.spec.template.spec.containers.append(container)
    pod.spec.containers.append(copy.deepcopy(container))
    status = copy.deepcopy(pod.status.container_statuses[0])
    status.name = "order-creator"
    pod.status.container_statuses.append(status)
    assert oracle.evaluate()["success"]
    assert clock.now == 600


def test_initial_terminating_pod_drains_before_stability_window(case):
    oracle, resources, clock = case
    old_pod = copy.deepcopy(resources["checkout"][1])
    old_pod.metadata.uid = "old-checkout"
    old_pod.metadata.deletion_timestamp = datetime.now(UTC)

    def pods(deployment, _namespace):
        current = resources[deployment.metadata.name][1]
        if deployment.metadata.name == "checkout" and clock.now < 15:
            return [old_pod, current]
        return [current]

    oracle.problem.kubectl.get_deployment_pods.side_effect = pods
    assert oracle.evaluate() == {"success": True, "stable_seconds": 600}
    assert clock.now == 615


@pytest.mark.parametrize("rollout_seconds", [0, 290, 300])
def test_initial_pod_drain_shares_bounded_rollout_grace(case, monkeypatch, rollout_seconds):
    oracle, resources, clock = case
    resources["checkout"][1].metadata.deletion_timestamp = datetime.now(UTC)
    monkeypatch.setattr(MitigationOracle, "_wait_for_rollouts", lambda *_: clock.sleep(rollout_seconds))
    result = oracle.evaluate()
    assert not result["success"]
    assert "stable set" in result["reason"]
    assert clock.now == 300


@pytest.mark.parametrize("at", [125, 210, 590, 600])
def test_rejects_restart_after_old_deadline_and_at_final_sample(case, at):
    oracle, resources, clock = case

    def restart():
        if clock.now >= at:
            resources["kafka"][1].status.container_statuses[0].restart_count = 1

    clock.on_sleep = restart
    result = oracle.evaluate()
    assert not result["success"]
    assert "restarted" in result["reason"]
    assert clock.now == at


@pytest.mark.parametrize("change", ["pod_uid", "container_id", "deployment_uid", "generation", "not_ready"])
def test_rejects_replacement_or_unready_workload_during_observation(case, change):
    oracle, resources, clock = case
    dep, pod = resources["kafka"]

    def mutate():
        if change == "pod_uid":
            pod.metadata.uid = "replacement"
        elif change == "container_id":
            pod.status.container_statuses[0].container_id = "replacement"
        elif change == "deployment_uid":
            dep.metadata.uid = "replacement"
        elif change == "generation":
            dep.metadata.generation = dep.status.observed_generation = 2
        else:
            pod.status.container_statuses[0].ready = False

    clock.on_sleep = mutate
    assert not oracle.evaluate()["success"]
    assert clock.now == 5


@pytest.mark.parametrize("name", ["checkout", "kafka"])
@pytest.mark.parametrize("condition", ["zero", "partial", "old_generation", "unavailable", "no_pods"])
def test_rejects_missing_capacity_and_incomplete_rollouts(case, name, condition):
    oracle, resources, clock = case
    dep, _ = resources[name]
    if condition == "zero":
        dep.spec.replicas = 0
    elif condition == "partial":
        dep.spec.replicas = 2
    elif condition == "old_generation":
        dep.metadata.generation = 2
    elif condition == "unavailable":
        dep.status.available_replicas = 0
    else:
        oracle.problem.kubectl.get_deployment_pods.return_value = []
        oracle.problem.kubectl.get_deployment_pods.side_effect = None
    assert not oracle.evaluate()["success"]
    assert clock.now == 0


@pytest.mark.parametrize("where", ["template", "pod"])
@pytest.mark.parametrize("change", ["heap", "memory", "missing_heap", "duplicate_heap", "missing_broker"])
def test_rejects_resource_changes_in_template_and_live_pod(case, where, change):
    oracle, resources, clock = case
    dep, pod = resources["kafka"]
    containers = dep.spec.template.spec.containers if where == "template" else pod.spec.containers
    container = containers[0]
    if change == "heap":
        container.env[0].value = "-Xmx800M"
    elif change == "memory":
        container.resources.limits["memory"] = "1200Mi"
    elif change == "missing_heap":
        container.env = []
    elif change == "duplicate_heap":
        container.env.append(copy.deepcopy(container.env[0]))
    else:
        container.name = "unrelated"
    assert not oracle.evaluate()["success"]
    assert clock.now == 0


@pytest.mark.parametrize("status", [403, 404, 500])
def test_api_errors_return_failed_results(case, status):
    oracle, _, _ = case
    oracle.problem.kubectl.get_deployment.side_effect = client.ApiException(status=status)
    assert not oracle.evaluate()["success"]


def test_lost_collateral_deployment_fails_during_observation(case, monkeypatch):
    oracle, _, clock = case
    monkeypatch.setattr(MitigationOracle, "_evaluate_current_state", lambda *_: {"success": clock.now < 10})
    assert not oracle.evaluate()["success"]
    assert clock.now == 10
