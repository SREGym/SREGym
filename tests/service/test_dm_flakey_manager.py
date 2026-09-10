import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from sregym.generators.fault.inject_kernel import KernelInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.dm_flakey_manager import DmFlakeyManager


def test_device_identity_is_stable_and_isolates_clusters():
    def manager(uid):
        return DmFlakeyManager(
            SimpleNamespace(exec_command_checked=lambda command: json.dumps({"metadata": {"uid": uid}}))
        )

    assert manager("uid-a").device_name("kind-worker") == manager("uid-a").device_name("kind-worker")
    assert manager("uid-a").device_name("kind-worker") != manager("uid-b").device_name("kind-worker")


def test_partial_setup_rolls_back_failed_node_and_previous_nodes(monkeypatch):
    manager = DmFlakeyManager(None)
    setup = Mock(side_effect=[None, RuntimeError("create failed")])
    rollback = Mock()
    monkeypatch.setattr(manager, "_setup_dm_flakey_on_node", setup)
    monkeypatch.setattr(manager, "teardown_openebs_dm_flakey_infrastructure", rollback)
    with pytest.raises(RuntimeError, match="create failed"):
        manager.setup_openebs_dm_flakey_infrastructure(["worker-a", "worker-b", "worker-c"])
    rollback.assert_called_once_with(["worker-b", "worker-a"])
    assert setup.call_count == 2


def test_teardown_errors_are_not_silently_successful(monkeypatch):
    manager = DmFlakeyManager(None)
    teardown = Mock(side_effect=[RuntimeError("device busy"), None])
    monkeypatch.setattr(manager, "_teardown_dm_flakey_on_node", teardown)
    with pytest.raises(RuntimeError, match="worker-a: device busy"):
        manager.teardown_openebs_dm_flakey_infrastructure(["worker-a", "worker-b"])
    assert teardown.call_count == 2


def test_remote_operation_has_timeout_and_propagates_failure(monkeypatch):
    manager = DmFlakeyManager(None)
    monkeypatch.setattr(manager, "_get_khaos_pod_on_node", lambda node: "khaos-test")
    run = Mock(return_value=SimpleNamespace(returncode=124, stdout="", stderr="timed out"))
    monkeypatch.setattr("sregym.service.dm_flakey_manager.subprocess.run", run)
    with pytest.raises(RuntimeError, match="exit 124"):
        manager._run_on_node("worker", "echo test")
    command = run.call_args.args[0]
    assert command[command.index("timeout") + 1 :][:2] == ["--kill-after=5", "120"]
    assert run.call_args.kwargs["timeout"] > 125


def test_fault_storage_override_does_not_change_source_manifests(tmp_path):
    source = tmp_path / "resources.yaml"
    source.write_text("kind: PersistentVolumeClaim\nmetadata:\n  name: database\nspec:\n  storageClassName: original\n")
    app = object.__new__(HotelReservation)
    app.k8s_deploy_path = tmp_path
    app.deployment_env_overrides = {}
    app.storage_class_name = "sregym-dm-flakey"
    with app._rendered_deployment_configs() as rendered:
        assert yaml.safe_load((rendered / source.name).read_text())["spec"]["storageClassName"] == "sregym-dm-flakey"
    assert yaml.safe_load(source.read_text())["spec"]["storageClassName"] == "original"


def test_kernel_reload_failure_is_not_reported_as_success(monkeypatch):
    kubectl = SimpleNamespace(exec_command_checked=Mock(side_effect=RuntimeError("Invalid argument")))
    injector = KernelInjector(kubectl)
    monkeypatch.setattr(injector, "_get_khaos_pod_on_node", lambda node: "khaos-test")
    with pytest.raises(RuntimeError, match="Invalid argument"):
        injector.dm_flakey_reload("worker", "test-device", 0, 1, "random_read_corrupt 1000000000")
