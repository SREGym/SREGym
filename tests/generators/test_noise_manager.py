from unittest.mock import patch

from sregym.generators.noise import manager
from sregym.generators.noise.catalog import EXPERIMENT_CATALOG


def test_noise_catalog_excludes_visible_pause_image_replacement():
    assert {entry["name"] for entry in EXPERIMENT_CATALOG} == {"pod-kill", "network-delay", "network-loss"}


def test_existing_chaos_install_requires_both_rollouts():
    def execute(command):
        if command.startswith("kubectl get ns"):
            return "chaos-mesh Active"
        if command.startswith("kubectl get deployment"):
            return "deployment.apps/chaos-controller-manager"
        return ""

    with patch.object(manager.NoiseManager, "_instance", None), patch.object(manager, "KubeCtl") as kubectl:
        kubectl.return_value.exec_command.side_effect = execute
        kubectl.return_value.exec_command_checked.side_effect = [None, RuntimeError("daemon rollout timed out")]
        noise = manager.NoiseManager()
        noise._chaos_mesh_ready = True
        noise._ensure_chaos_mesh_installed()

    assert noise._chaos_mesh_ready is False
    commands = [call.args[0] for call in kubectl.return_value.exec_command_checked.call_args_list]
    assert "deployment/chaos-controller-manager" in commands[0]
    assert "daemonset/chaos-daemon" in commands[1]
    assert all(call.kwargs["timeout"] >= 180 for call in kubectl.return_value.exec_command_checked.call_args_list)


def test_existing_chaos_install_becomes_ready_after_both_rollouts():
    def execute(command):
        if command.startswith("kubectl get ns"):
            return "chaos-mesh Active"
        if command.startswith("kubectl get deployment"):
            return "deployment.apps/chaos-controller-manager"
        return ""

    with patch.object(manager.NoiseManager, "_instance", None), patch.object(manager, "KubeCtl") as kubectl:
        kubectl.return_value.exec_command.side_effect = execute
        noise = manager.NoiseManager()
        noise._ensure_chaos_mesh_installed()

    assert noise._chaos_mesh_ready is True
    assert kubectl.return_value.exec_command_checked.call_count == 2
