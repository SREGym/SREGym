"""Protect baseline resources and current ConfigMap dependencies, not retired names."""

from types import SimpleNamespace

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles import mitigation
from sregym.conductor.oracles.mitigation import MitigationOracle


@pytest.fixture(autouse=True)
def _fast_rollout_settle(monkeypatch):
    monkeypatch.setattr(mitigation, "_ROLLOUT_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(mitigation, "_ROLLOUT_POLL_INTERVAL", 0.01)


def _object(name, deleting=False):
    return SimpleNamespace(metadata=SimpleNamespace(name=name, deletion_timestamp="now" if deleting else None))


def _pod_spec(configmap="mongo-geo-script"):
    return client.V1PodSpec(
        containers=[client.V1Container(name="c")],
        volumes=[client.V1Volume(name="settings", config_map=client.V1ConfigMapVolumeSource(name=configmap))]
        if configmap
        else [],
    )


def _deployment(name):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, generation=1),
        spec=SimpleNamespace(replicas=1, template=SimpleNamespace(spec=_pod_spec())),
        status=SimpleNamespace(
            replicas=1,
            observed_generation=1,
            available_replicas=1,
            updated_replicas=1,
            ready_replicas=1,
            unavailable_replicas=0,
        ),
    )


def _pod(name):
    ready = SimpleNamespace(name="c", ready=True, state=SimpleNamespace(waiting=None, terminated=None))
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=_pod_spec(),
        status=SimpleNamespace(phase="Running", container_statuses=[ready]),
    )


class _Cluster:
    """Healthy app: two Deployments with Ready pods, plus the objects they use."""

    def __init__(self):
        self.objects = {
            "service": [_object("frontend"), _object("mongodb-reservation")],
            "persistent_volume_claim": [_object("reservation-pvc")],
            "config_map": [_object("mongo-geo-script")],
            "stateful_set": [_object("opensearch")],
        }
        self.deployments = [_deployment("frontend"), _deployment("reservation")]
        self.pods = [_pod("frontend-1"), _pod("reservation-1")]
        self.core_v1_api = self.apps_v1_api = self

    def __getattr__(self, name):
        kind = name.removeprefix("list_namespaced_")
        return lambda namespace: SimpleNamespace(items=list(self.objects[kind]))

    def list_deployments(self, namespace):
        return SimpleNamespace(items=list(self.deployments))

    def list_pods(self, namespace):
        return SimpleNamespace(items=list(self.pods))

    def remove(self, kind, name):
        self.objects[kind] = [o for o in self.objects[kind] if o.metadata.name != name]

    def use_configmap(self, name):
        for deployment in self.deployments:
            deployment.spec.template.spec = _pod_spec(name)
        for pod in self.pods:
            pod.spec = _pod_spec(name)


@pytest.fixture
def cluster():
    return _Cluster()


@pytest.fixture
def oracle(cluster):
    oracle = MitigationOracle(SimpleNamespace(kubectl=cluster, namespace="hotel-reservation"))
    oracle.capture_baseline()
    return oracle


def test_healthy_app_passes(oracle):
    assert oracle.evaluate() == {"success": True}


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("service", "mongodb-reservation"),
        ("persistent_volume_claim", "reservation-pvc"),
        ("config_map", "mongo-geo-script"),
        ("stateful_set", "opensearch"),
    ],
)
def test_deleted_baseline_object_fails(oracle, cluster, kind, name):
    cluster.remove(kind, name)
    verdict = oracle.evaluate()
    assert verdict["success"] is False
    assert verdict["reason"] == "required_resource_missing"
    assert verdict["failure_class"] == "agent_error"


def test_pvc_stuck_terminating_fails(oracle, cluster):
    """`kubectl delete pvc` on a mounted claim leaves it Terminating: still a deletion."""
    cluster.objects["persistent_volume_claim"] = [_object("reservation-pvc", deleting=True)]
    assert oracle.evaluate()["reason"] == "required_resource_missing"


def test_objects_added_after_the_baseline_are_allowed(oracle, cluster):
    cluster.objects["config_map"].append(_object("frontend-env"))
    cluster.objects["service"].append(_object("frontend-canary"))
    assert oracle.evaluate() == {"success": True}


def test_equivalent_configmap_replacement_passes(oracle, cluster):
    cluster.objects["config_map"] = [_object("mongo-geo-init")]
    cluster.use_configmap("mongo-geo-init")
    assert oracle.evaluate() == {"success": True}


def test_removed_unused_configmap_passes(cluster):
    cluster.objects["config_map"].append(_object("unused-settings"))
    oracle = MitigationOracle(SimpleNamespace(kubectl=cluster, namespace="hotel-reservation"))
    oracle.capture_baseline()
    cluster.remove("config_map", "unused-settings")
    assert oracle.evaluate() == {"success": True}


def test_replacement_configmap_must_still_exist(oracle, cluster):
    cluster.use_configmap("mongo-geo-init")
    cluster.remove("config_map", "mongo-geo-script")
    verdict = oracle.evaluate()
    assert verdict["reason"] == "required_resource_missing"
    assert verdict["detail"]["name"] == "mongo-geo-init"


def test_terminating_referenced_configmap_fails(oracle, cluster):
    cluster.objects["config_map"] = [_object("mongo-geo-script", deleting=True)]
    assert oracle.evaluate()["reason"] == "required_resource_missing"


@pytest.fixture(params=["volume", "projected", "env", "env_from", "init-env", "init-env_from"])
def configmap_reference(request):
    spec = _pod_spec(None)
    if request.param in ("volume", "projected"):
        if request.param == "volume":
            reference = client.V1ConfigMapVolumeSource(name="mongo-geo-script")
            volume = client.V1Volume(name="settings", config_map=reference)
        else:
            reference = client.V1ConfigMapProjection(name="mongo-geo-script")
            volume = client.V1Volume(
                name="settings",
                projected=client.V1ProjectedVolumeSource(sources=[client.V1VolumeProjection(config_map=reference)]),
            )
        spec.volumes = [volume]
    else:
        container = spec.containers[0]
        if request.param.startswith("init-"):
            spec.init_containers = [container]
            spec.containers = [client.V1Container(name="app")]
        if request.param.endswith("env_from"):
            reference = client.V1ConfigMapEnvSource(name="mongo-geo-script")
            container.env_from = [client.V1EnvFromSource(config_map_ref=reference)]
        else:
            reference = client.V1ConfigMapKeySelector(name="mongo-geo-script", key="setting")
            container.env = [
                client.V1EnvVar(name="SETTING", value_from=client.V1EnvVarSource(config_map_key_ref=reference))
            ]
    return spec, reference


@pytest.mark.parametrize("location", ["pod", "deployment", "statefulset"])
@pytest.mark.parametrize("optional", [False, True])
def test_current_configmap_references(oracle, cluster, configmap_reference, location, optional):
    spec, reference = configmap_reference
    reference.optional = optional
    cluster.use_configmap(None)
    if location == "pod":
        cluster.pods[0].spec = spec
    elif location == "deployment":
        cluster.deployments[0].spec.template.spec = spec
    else:
        cluster.objects["stateful_set"][0].spec = SimpleNamespace(template=SimpleNamespace(spec=spec))
    cluster.remove("config_map", "mongo-geo-script")
    verdict = oracle.evaluate()
    assert verdict["success"] is optional
    if not optional:
        assert verdict["reason"] == "required_resource_missing"
        assert verdict["failure_class"] == "agent_error"
        assert verdict["detail"]["kind"] == "ConfigMap"


def test_completed_job_does_not_keep_retired_configmap_required(oracle, cluster):
    completed = _pod("completed-job")
    completed.metadata.owner_references = [SimpleNamespace(kind="Job")]
    completed.status.phase = "Succeeded"
    cluster.pods.append(completed)
    cluster.objects["config_map"] = [_object("mongo-geo-init")]
    cluster.use_configmap("mongo-geo-init")
    completed.spec = _pod_spec()
    assert oracle.evaluate() == {"success": True}


def test_inactive_optional_workload_does_not_require_configmap(cluster):
    cluster.use_configmap(None)
    cluster.deployments[0].spec.replicas = 0
    cluster.deployments[0].status.replicas = 0
    cluster.deployments[0].status.updated_replicas = 0
    cluster.deployments[0].status.ready_replicas = 0
    cluster.deployments[0].status.available_replicas = 0
    cluster.deployments[0].spec.template.spec = _pod_spec()
    cluster.pods.pop(0)
    cluster.objects["config_map"] = []
    oracle = MitigationOracle(SimpleNamespace(kubectl=cluster, namespace="hotel-reservation"))
    oracle.capture_baseline()
    assert oracle.evaluate() == {"success": True}


def test_configmap_api_error_keeps_environment_classification(oracle, cluster, monkeypatch):
    def unavailable(namespace):
        raise ApiException(status=503, reason="Unavailable")

    monkeypatch.setattr(cluster, "list_namespaced_config_map", unavailable)
    with pytest.raises(ApiException) as error:
        oracle.evaluate()
    assert oracle.fail_from_exception(error.value)["failure_class"] == "environment_error"
