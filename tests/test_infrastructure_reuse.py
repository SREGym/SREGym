import json
import logging
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from kubernetes.client.rest import ApiException

from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor
from sregym.service import helm as helm_module
from sregym.service.cluster_state import ClusterBaseline, ClusterStateManager
from sregym.service.helm import Helm


@pytest.fixture
def conductor():
    obj = Conductor.__new__(Conductor)
    obj.logger = logging.getLogger("test.infra")
    obj.kubectl = MagicMock()
    obj.kubectl.assert_pod_networking = MagicMock()
    obj.kubectl.exec_command.return_value = json.dumps(
        {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
            },
        }
    )
    return obj


@pytest.mark.parametrize(
    "response,healthy",
    [
        (json.dumps({"kind": "NodeMetricsList", "items": [{"metadata": {"name": "node"}}]}), True),
        (json.dumps({"kind": "NodeMetricsList", "items": []}), False),
        (json.dumps({"kind": "Status", "status": "Failure"}), False),
        ("Error from server (ServiceUnavailable)", False),
        ("null", False),
    ],
)
def test_metrics_reuse_requires_the_functional_api(conductor, response, healthy):
    conductor.kubectl.exec_command.side_effect = [" ".join(conductor._METRICS_SERVER_ARGS), "binding", response]
    assert conductor._metrics_server_configured() is healthy
    assert "--raw /apis/metrics.k8s.io/v1beta1/nodes" in conductor.kubectl.exec_command.call_args.args[0]


@pytest.mark.parametrize(
    "changed",
    [
        {},
        {"desired_number_scheduled": 0, "current_number_scheduled": 0, "number_ready": 0},
        {"number_ready": 2},
        {"number_available": 2},
        {"updated_number_scheduled": 2},
        {"observed_generation": 1},
        {"number_misscheduled": 1},
    ],
)
def test_ndm_reuse_requires_a_nonempty_current_rollout(conductor, changed):
    status = dict(
        desired_number_scheduled=3,
        current_number_scheduled=3,
        updated_number_scheduled=3,
        number_ready=3,
        number_available=3,
        observed_generation=2,
        number_misscheduled=0,
    )
    status.update(changed)
    conductor.kubectl.apps_v1_api.read_namespaced_daemon_set.return_value = SimpleNamespace(
        metadata=SimpleNamespace(generation=2), status=SimpleNamespace(**status)
    )
    assert conductor._openebs_ready(svelte=False) is (not changed)


def test_svelte_does_not_require_ndm(conductor):
    assert conductor._openebs_ready(svelte=True)
    conductor.kubectl.apps_v1_api.read_namespaced_daemon_set.assert_not_called()


def test_missing_ndm_is_not_reused(conductor):
    conductor.kubectl.apps_v1_api.read_namespaced_daemon_set.side_effect = ApiException(status=404)
    assert not conductor._openebs_ready(svelte=False)


def test_new_ndm_without_status_is_not_reused(conductor):
    conductor.kubectl.apps_v1_api.read_namespaced_daemon_set.return_value = SimpleNamespace(status=None)
    assert not conductor._openebs_ready(svelte=False)


class ObserverSetupReached(Exception):
    pass


def openebs_workload(container_name, timeout_seconds):
    container = SimpleNamespace(name=container_name, liveness_probe=SimpleNamespace(timeout_seconds=timeout_seconds))
    return SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container]))))


@pytest.fixture
def startup(conductor, monkeypatch):
    conductor._baseline_captured = True
    conductor.problem = SimpleNamespace()
    conductor.prometheus = MagicMock()
    conductor.prometheus.deploy.side_effect = ObserverSetupReached
    conductor._metrics_server_configured = MagicMock(return_value=True)
    conductor._openebs_ready = MagicMock(return_value=True)
    conductor._preflight_openebs_udev_mount = MagicMock()
    conductor._trim_openebs_ndm = MagicMock()
    conductor._preload_openebs_helper_image = MagicMock()
    conductor._ensure_openebs_device_storageclass = MagicMock()
    conductor.kubectl.apps_v1_api.read_namespaced_deployment.return_value = openebs_workload(
        "openebs-provisioner-hostpath", 10
    )
    conductor.kubectl.apps_v1_api.read_namespaced_daemon_set.return_value = openebs_workload("node-disk-manager", 15)
    monkeypatch.setattr(conductor_module, "is_svelte", lambda: False)
    monkeypatch.setattr(conductor_module.time, "sleep", MagicMock())
    return conductor


@pytest.mark.parametrize("svelte", [False, True])
def test_startup_repairs_the_metrics_selector_then_waits_for_metrics(startup, monkeypatch, svelte):
    monkeypatch.setattr(conductor_module, "is_svelte", lambda: svelte)
    startup._metrics_server_configured.side_effect = [False, False, True]
    with pytest.raises(ObserverSetupReached):
        startup.deploy_app()
    startup.kubectl.core_v1_api.patch_namespaced_service.assert_called_once_with(
        "metrics-server",
        "kube-system",
        [{"op": "replace", "path": "/spec/selector", "value": {"k8s-app": "metrics-server"}}],
        _request_timeout=10,
    )
    assert startup._metrics_server_configured.call_count == 3
    conductor_module.time.sleep.assert_called_once_with(2)


@pytest.mark.parametrize("svelte", [False, True])
def test_startup_repairs_ndm_scheduling_only_in_full(startup, monkeypatch, svelte):
    monkeypatch.setattr(conductor_module, "is_svelte", lambda: svelte)
    startup._openebs_ready.side_effect = [False, False, True]
    startup.kubectl.apps_v1_api.read_namespaced_deployment.return_value = openebs_workload(
        "openebs-provisioner-hostpath", 1
    )
    startup.kubectl.apps_v1_api.read_namespaced_daemon_set.return_value = openebs_workload("node-disk-manager", 1)
    with pytest.raises(ObserverSetupReached):
        startup.deploy_app()
    patch = startup.kubectl.apps_v1_api.patch_namespaced_daemon_set
    deploy_patch = startup.kubectl.apps_v1_api.patch_namespaced_deployment
    deploy_patch.assert_called_once_with(
        "openebs-localpv-provisioner",
        "openebs",
        [
            {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "openebs-provisioner-hostpath"},
            {"op": "add", "path": "/spec/template/spec/containers/0/livenessProbe/timeoutSeconds", "value": 10},
        ],
        _request_timeout=10,
    )
    if svelte:
        patch.assert_not_called()
        startup._trim_openebs_ndm.assert_called_once()
        startup._ensure_openebs_device_storageclass.assert_not_called()
    else:
        assert patch.call_count == 2
        patch.assert_any_call(
            "openebs-ndm",
            "openebs",
            [{"op": "add", "path": "/spec/template/spec/nodeSelector", "value": {}}],
            _request_timeout=10,
        )
        patch.assert_any_call(
            "openebs-ndm",
            "openebs",
            [
                {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "node-disk-manager"},
                {"op": "add", "path": "/spec/template/spec/containers/0/livenessProbe/timeoutSeconds", "value": 10},
            ],
            _request_timeout=10,
        )
        startup._ensure_openebs_device_storageclass.assert_called_once()
    assert startup._openebs_ready.call_count == 3


def test_healthy_infrastructure_is_not_patched(startup):
    with pytest.raises(ObserverSetupReached):
        startup.deploy_app()
    startup.kubectl.assert_pod_networking.assert_called_once_with()
    startup.kubectl.core_v1_api.patch_namespaced_service.assert_not_called()
    startup.kubectl.apps_v1_api.patch_namespaced_deployment.assert_not_called()
    startup.kubectl.apps_v1_api.patch_namespaced_daemon_set.assert_not_called()
    assert startup._metrics_server_configured.call_count == 2
    assert startup._openebs_ready.call_count == 2


def test_startup_updates_reused_openebs_without_reinstalling(startup):
    apps = startup.kubectl.apps_v1_api
    apps.read_namespaced_deployment.return_value = openebs_workload("openebs-provisioner-hostpath", 1)
    with pytest.raises(ObserverSetupReached):
        startup.deploy_app()
    apps.patch_namespaced_deployment.assert_called_once()
    apps.patch_namespaced_daemon_set.assert_not_called()
    assert not any("openebs-operator.yaml" in call.args[0] for call in startup.kubectl.exec_command.call_args_list)
    startup._preload_openebs_helper_image.assert_called_once_with()


def test_networking_failure_stops_before_infrastructure_or_application(startup):
    startup.kubectl.assert_pod_networking.side_effect = RuntimeError("worker-b: FailedCreatePodSandBox: Unauthorized")

    with pytest.raises(RuntimeError, match="worker-b.*Unauthorized"):
        startup.deploy_app()

    startup._metrics_server_configured.assert_not_called()
    startup.kubectl.exec_command.assert_not_called()
    startup.prometheus.deploy.assert_not_called()


@pytest.mark.parametrize("svelte", [False, True])
def test_startup_removes_interrupted_preloads_before_waiting_for_openebs(startup, monkeypatch, svelte):
    monkeypatch.setattr(conductor_module, "is_svelte", lambda: svelte)
    events = MagicMock()
    events.attach_mock(startup.kubectl.exec_command_checked, "command")
    events.attach_mock(startup.kubectl.wait_for_ready, "wait")

    with pytest.raises(ObserverSetupReached):
        startup.deploy_app()

    cleanup_index = next(
        index
        for index, call in enumerate(events.mock_calls)
        if call[0] == "command" and "-l app=sregym-image-preload" in call.args[0]
    )
    wait_index = next(
        index for index, call in enumerate(events.mock_calls) if call[0] == "wait" and call.args == ("openebs",)
    )
    assert cleanup_index < wait_index
    assert events.mock_calls[cleanup_index].kwargs["timeout"] == conductor_module.WAIT_FOR_POD_READY_TIMEOUT + 10


def test_cleanup_openebs_helper_pods_deletes_only_orphaned_terminal_helpers(conductor):
    def make_pod(name, phase, container_name, *, owner_references=None, created_at=1):
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=name,
                uid=f"{name}-uid",
                owner_references=owner_references,
                labels={},
                creation_timestamp=created_at,
            ),
            status=SimpleNamespace(phase=phase),
            spec=SimpleNamespace(containers=[SimpleNamespace(name=container_name)]),
        )

    provisioner = make_pod("provisioner", "Running", "openebs-provisioner-hostpath")
    provisioner.metadata.labels["name"] = "openebs-localpv-provisioner"
    provisioner.status.container_statuses = [
        SimpleNamespace(
            name="openebs-provisioner-hostpath", state=SimpleNamespace(running=SimpleNamespace(started_at=2))
        )
    ]
    conductor.kubectl.core_v1_api.list_namespaced_pod.return_value = SimpleNamespace(
        items=[
            provisioner,
            make_pod("init-pvc-111", "Succeeded", "local-path-init"),
            make_pod("cleanup-pvc-222", "Failed", "local-path-cleanup"),
            make_pod("init-pvc-active", "Running", "local-path-init"),
            make_pod("init-pvc-finished-for-current-process", "Succeeded", "local-path-init", created_at=3),
            make_pod("init-pvc-unknown-age", "Succeeded", "local-path-init", created_at=None),
            make_pod("init-pvc-owned", "Succeeded", "local-path-init", owner_references=[SimpleNamespace(kind="Job")]),
            make_pod("other-pod", "Succeeded", "local-path-init"),
            make_pod("init-pvc-wrong-container", "Succeeded", "unexpected"),
        ]
    )

    conductor._cleanup_openebs_helper_pods()

    assert [
        (call.kwargs["name"], call.kwargs["namespace"], call.kwargs["body"])
        for call in conductor.kubectl.core_v1_api.delete_namespaced_pod.call_args_list
    ] == [
        ("init-pvc-111", "openebs", {"preconditions": {"uid": "init-pvc-111-uid"}}),
        ("cleanup-pvc-222", "openebs", {"preconditions": {"uid": "cleanup-pvc-222-uid"}}),
    ]
    provisioner.status.container_statuses[0].state.running = None
    conductor.kubectl.core_v1_api.delete_namespaced_pod.reset_mock()
    conductor._cleanup_openebs_helper_pods()
    conductor.kubectl.core_v1_api.delete_namespaced_pod.assert_not_called()


def test_startup_cleans_orphaned_openebs_helpers_before_waiting_for_openebs(startup):
    events = MagicMock()
    events.attach_mock(startup.kubectl.core_v1_api.list_namespaced_pod, "list_openebs_pods")
    events.attach_mock(startup.kubectl.wait_for_ready, "wait")

    with pytest.raises(ObserverSetupReached):
        startup.deploy_app()

    list_index = next(
        index
        for index, call in enumerate(events.mock_calls)
        if call[0] == "list_openebs_pods" and call.args == ("openebs",)
    )
    wait_index = next(
        index for index, call in enumerate(events.mock_calls) if call[0] == "wait" and call.args == ("openebs",)
    )
    assert list_index < wait_index


def test_openebs_helper_image_is_discovered_and_preloaded(conductor):
    container = SimpleNamespace(
        env=[SimpleNamespace(name="OPENEBS_IO_HELPER_IMAGE", value="registry.example/linux-utils:v1")]
    )
    conductor.kubectl.apps_v1_api.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
    )

    conductor._preload_openebs_helper_image()

    conductor.kubectl.preload_image_on_nodes.assert_called_once_with(
        "registry.example/linux-utils:v1",
        namespace="openebs",
        timeout=conductor_module.WAIT_FOR_POD_READY_TIMEOUT,
    )


def test_missing_openebs_helper_image_stops_deployment(conductor):
    container = SimpleNamespace(env=[])
    conductor.kubectl.apps_v1_api.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
    )

    with pytest.raises(RuntimeError, match="does not declare OPENEBS_IO_HELPER_IMAGE"):
        conductor._preload_openebs_helper_image()


@pytest.mark.parametrize(
    "check,name", [("_metrics_server_configured", "metrics-server"), ("_openebs_ready", "OpenEBS")]
)
def test_failed_repair_stops_before_observers_and_application(startup, monkeypatch, check, name):
    getattr(startup, check).return_value = False
    monkeypatch.setattr(conductor_module.time, "monotonic", MagicMock(side_effect=[0, 180]))
    # An earlier healthy component can complete its own wait immediately.
    timeout = conductor_module.WAIT_FOR_POD_READY_TIMEOUT if name == "OpenEBS" else 180
    if name == "OpenEBS":
        conductor_module.time.monotonic.side_effect = [0, 0, timeout]
    with pytest.raises(RuntimeError, match=f"{name} did not become healthy within {timeout}s"):
        startup.deploy_app()
    startup.prometheus.deploy.assert_not_called()
    startup._ensure_openebs_device_storageclass.assert_not_called()


def test_repair_api_errors_stop_setup(startup):
    startup._metrics_server_configured.return_value = False
    startup.kubectl.core_v1_api.patch_namespaced_service.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        startup.deploy_app()
    startup.prometheus.deploy.assert_not_called()


def test_reconciliation_preserves_only_exact_infrastructure_identities():
    manager = ClusterStateManager.__new__(ClusterStateManager)
    manager.baseline = ClusterBaseline()
    for attribute in ("kubectl", "core_v1", "rbac_v1", "storage_v1", "apiextensions_v1", "admission_v1"):
        setattr(manager, attribute, MagicMock())
    manager.kubectl.gc_orphan_localpv_dirs.return_value = {}
    for method in (
        "_get_namespaces",
        "_get_persistent_volumes",
        "_get_node_labels",
        "_get_node_taints",
        "_get_validating_webhook_configs",
        "_get_mutating_webhook_configs",
        "_get_cluster_roles",
        "_get_cluster_role_bindings",
        "_get_crds",
        "_get_storage_classes",
    ):
        setattr(manager, method, MagicMock(return_value=set()))
    manager._reconcile_node_labels = MagicMock(return_value={})
    manager._reconcile_node_taints = MagicMock(return_value={})
    manager._is_coredns_modified = MagicMock(return_value=False)
    manager._strip_cr_finalizers = MagicMock()
    decoys = {"debug-openebs-access", "unrelated-metrics-server-reader", "openebs-hostpath"}
    manager._get_cluster_roles.return_value = decoys | {"openebs-maya-operator", "system:metrics-server"}
    manager._get_cluster_role_bindings.return_value = decoys | {
        "openebs-maya-operator",
        "metrics-server:system:auth-delegator",
        "system:metrics-server",
    }
    manager._get_storage_classes.return_value = {"openebs-hostpath", "openebs-device", "other-openebs-class"}
    manager._get_crds.return_value = {"blockdevices.openebs.io", "blockdeviceclaims.openebs.io", "other.openebs.io"}
    manager._get_validating_webhook_configs.return_value = {"openebs-maya-operator"}
    manager._get_mutating_webhook_configs.return_value = {"openebs-maya-operator"}

    changes = manager.reconcile_to_baseline()

    assert set(changes["cluster_roles_deleted"]) == decoys
    assert set(changes["cluster_role_bindings_deleted"]) == decoys
    assert changes["storage_classes_deleted"] == ["other-openebs-class"]
    assert changes["crds_deleted"] == ["other.openebs.io"]
    assert changes["validating_webhook_configs_deleted"] == ["openebs-maya-operator"]
    assert changes["mutating_webhook_configs_deleted"] == ["openebs-maya-operator"]


@pytest.fixture
def chart(tmp_path):
    def create(dependencies, api_version="v2"):
        metadata = {"apiVersion": api_version, "name": "test", "version": "1.0.0"}
        if api_version == "v1":
            (tmp_path / "requirements.yaml").write_text(yaml.safe_dump({"dependencies": dependencies}))
        else:
            metadata["dependencies"] = dependencies
        (tmp_path / "Chart.yaml").write_text(yaml.safe_dump(metadata))
        return str(tmp_path)

    return create


@pytest.mark.parametrize("repository_column", ["\t", ""])
@pytest.mark.parametrize("api_version", ["v1", "v2"])
def test_helm_accepts_local_dependencies_and_ignores_warnings(monkeypatch, chart, repository_column, api_version):
    path = chart([{"name": "local"}, {"name": "remote"}], api_version)
    output = (
        "NAME\tVERSION\tREPOSITORY\tSTATUS\n"
        f"local\t1.0.0\t{repository_column}unpacked\n"
        "remote\t1.0.0\thttps://charts.example\tok\n\n"
        'WARNING: "charts/extra" is not in Chart.yaml.\n'
    )
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""))
    monkeypatch.setattr(helm_module.subprocess, "run", run)
    Helm.ensure_dependencies(path)
    run.assert_called_once_with(["helm", "dependency", "list", path], capture_output=True, text=True)


def test_chart_without_dependencies_does_not_contact_repositories(monkeypatch, chart):
    run = MagicMock()
    monkeypatch.setattr(helm_module.subprocess, "run", run)
    Helm.ensure_dependencies(chart([]))
    run.assert_not_called()


@pytest.mark.parametrize("local_status", ["missing", "wrong version", None])
def test_helm_updates_when_any_local_dependency_is_missing_or_incompatible(monkeypatch, chart, local_status):
    path = chart([{"name": "local"}, {"name": "remote"}])
    output = "NAME\tVERSION\tREPOSITORY\tSTATUS\nremote\t1\thttps://charts.example\tok\n"
    if local_status is not None:
        output += f"local\t1\t\t{local_status}\n"
    run = MagicMock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=output, stderr=""),
            SimpleNamespace(returncode=0, stdout="updated", stderr=""),
        ]
    )
    monkeypatch.setattr(helm_module.subprocess, "run", run)
    Helm.ensure_dependencies(path)
    assert run.call_args.args[0] == ["helm", "dependency", "update", path]


def test_failed_dependency_update_stops_before_install(monkeypatch, chart):
    path = chart([{"name": "missing"}])
    run = MagicMock(
        side_effect=[
            SimpleNamespace(returncode=1, stdout="", stderr="list failed"),
            SimpleNamespace(returncode=1, stdout="partial update", stderr="repository failed"),
        ]
    )
    monkeypatch.setattr(helm_module.subprocess, "run", run)
    popen = MagicMock()
    monkeypatch.setattr(helm_module.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError) as error:
        Helm.install(release_name="test", namespace="test", chart_path=path)
    assert path in str(error.value)
    assert "partial update" in str(error.value)
    assert "repository failed" in str(error.value)
    popen.assert_not_called()


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm CLI is not installed")
def test_real_helm_missing_local_chart_stops_before_install(monkeypatch, chart):
    path = chart([{"name": "missing-local", "version": "1.0.0"}])
    popen = MagicMock()
    # dependency list/update use subprocess.run, which needs the real Popen.
    # Intercept only the eventual shell-based install command.
    original = helm_module.subprocess.Popen

    def no_install(command, *args, **kwargs):
        if isinstance(command, str) and command.startswith("helm install "):
            return popen(command, *args, **kwargs)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(helm_module.subprocess, "Popen", no_install)
    with pytest.raises(RuntimeError, match="dependency update failed"):
        Helm.install(release_name="test", namespace="test", chart_path=path)
    popen.assert_not_called()
