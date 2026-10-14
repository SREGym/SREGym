import concurrent.futures
import json
import shlex
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from sregym.generators.noise import manager
from sregym.generators.noise.catalog import EXPERIMENT_CATALOG


def test_noise_catalog_excludes_visible_pause_image_replacement():
    assert {entry["name"] for entry in EXPERIMENT_CATALOG} == {"pod-kill", "network-delay", "network-loss"}


@pytest.fixture
def installed_noise():
    with patch.object(manager.NoiseManager, "_instance", None), patch.object(manager, "KubeCtl") as kubectl:
        kubectl.return_value.exec_command.side_effect = lambda command: (
            "chaos-mesh Active" if command.startswith("kubectl get ns") else "deployment.apps/chaos-controller-manager"
        )
        daemon = {"metadata": {"resourceVersion": "123"}, "spec": {"template": {"spec": {"tolerations": []}}}}
        nodes = {
            "items": [
                {"metadata": {"name": name, "labels": {"kubernetes.io/os": "linux"}}}
                for name in ("kind-control-plane", "kind-worker")
            ]
        }
        endpoints = {
            "items": [
                {
                    "endpoints": [
                        {"nodeName": n["metadata"]["name"], "addresses": [f"10.0.0.{i}"], "conditions": {"ready": True}}
                        for i, n in enumerate(nodes["items"], 1)
                    ]
                }
            ]
        }

        def checked(command, **kwargs):
            if command.startswith("kubectl get daemonset"):
                return json.dumps(daemon)
            if command.startswith("kubectl get nodes"):
                return json.dumps(nodes)
            if command.startswith("kubectl get endpointslices"):
                return json.dumps(endpoints)
            return ""

        kubectl.return_value.exec_command_checked.side_effect = checked
        noise = manager.NoiseManager()
        yield noise, daemon, nodes, endpoints


def test_existing_chaos_install_requires_both_rollouts(installed_noise):
    noise, *_ = installed_noise
    execute = noise.kubectl.exec_command_checked.side_effect

    def checked(command, **kwargs):
        if "rollout status daemonset" in command:
            raise RuntimeError("daemon rollout timed out")
        return execute(command, **kwargs)

    noise.kubectl.exec_command_checked.side_effect = checked
    noise._chaos_mesh_ready = True
    noise._ensure_chaos_mesh_installed()
    assert noise._chaos_mesh_ready is False
    rollouts = [c for c in noise.kubectl.exec_command_checked.call_args_list if "rollout status" in c.args[0]]
    assert len(rollouts) == 2
    assert "deployment/chaos-controller-manager" in rollouts[0].args[0]
    assert "daemonset/chaos-daemon" in rollouts[1].args[0]
    assert all(c.kwargs["timeout"] >= 180 for c in rollouts)


def test_existing_chaos_install_becomes_ready_after_node_coverage(installed_noise):
    noise, *_ = installed_noise
    noise._ensure_chaos_mesh_installed()
    assert noise._chaos_mesh_ready is True
    assert any("get endpointslices" in c.args[0] for c in noise.kubectl.exec_command_checked.call_args_list)


@pytest.mark.parametrize(
    "existing", [None, [], [{"key": "special", "operator": "Equal", "value": "yes", "effect": "NoSchedule"}]]
)
def test_daemon_reconciliation_preserves_existing_tolerations(installed_noise, existing):
    noise, daemon, *_ = installed_noise
    spec = daemon["spec"]["template"]["spec"]
    if existing is None:
        spec.pop("tolerations")
    else:
        spec["tolerations"] = existing
    noise._reconcile_daemon_tolerations()
    command = noise.kubectl.exec_command_checked.call_args.args[0]
    tokens = shlex.split(command)
    patch = json.loads(tokens[tokens.index("-p") + 1])
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "123"}
    assert patch[1]["path"] == "/spec/template/spec/tolerations" + ("/-" if existing is not None else "")
    assert patch[1]["value"] == ({"operator": "Exists"} if existing is not None else [{"operator": "Exists"}])
    assert spec.get("tolerations") == existing


def test_daemon_reconciliation_is_idempotent(installed_noise):
    noise, daemon, *_ = installed_noise
    daemon["spec"]["template"]["spec"]["tolerations"] = [{"operator": "Exists"}]
    noise._reconcile_daemon_tolerations()
    assert noise.kubectl.exec_command_checked.call_count == 1


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_successful_rollout_without_control_plane_daemon_is_not_ready(installed_noise, monkeypatch):
    noise, _, _, endpoints = installed_noise
    endpoints["items"][0]["endpoints"][0]["conditions"]["ready"] = False
    clock = Clock()
    monkeypatch.setattr(manager.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(manager.time, "sleep", clock.sleep)
    noise._ensure_chaos_mesh_installed()
    assert not noise._chaos_mesh_ready
    assert clock.now == 180


def test_coverage_waits_for_control_plane_endpoint(installed_noise, monkeypatch):
    noise, _, _, endpoints = installed_noise
    control_plane = endpoints["items"][0]["endpoints"].pop(0)
    clock = Clock()
    monkeypatch.setattr(manager.time, "monotonic", clock.monotonic)

    def sleep(seconds):
        clock.sleep(seconds)
        endpoints["items"][0]["endpoints"].append(control_plane)

    monkeypatch.setattr(manager.time, "sleep", sleep)
    noise.wait_for_chaos_mesh_ready()
    assert noise._chaos_mesh_ready
    assert clock.now == 2


def test_chart_allows_daemons_on_tainted_nodes():
    values = yaml.safe_load((Path(manager.__file__).parent / "impl" / "chaos-mesh-values.yaml").read_text())
    assert values["chaosDaemon"]["tolerations"] == [{"operator": "Exists"}]


def test_daemon_patch_error_does_not_mark_install_ready(installed_noise):
    noise, *_ = installed_noise
    execute = noise.kubectl.exec_command_checked.side_effect

    def checked(command, **kwargs):
        if command.startswith("kubectl patch"):
            raise RuntimeError("resourceVersion test failed: concurrent edit")
        return execute(command, **kwargs)

    noise.kubectl.exec_command_checked.side_effect = checked
    noise._ensure_chaos_mesh_installed()
    assert not noise._chaos_mesh_ready
    assert not any("rollout status" in c.args[0] for c in noise.kubectl.exec_command_checked.call_args_list)


def test_windows_nodes_do_not_require_a_linux_daemon(installed_noise):
    noise, _, nodes, _ = installed_noise
    nodes["items"].append({"metadata": {"name": "windows-worker", "labels": {"kubernetes.io/os": "windows"}}})
    noise._wait_for_daemon_coverage(timeout=0)


def test_no_linux_nodes_does_not_satisfy_coverage(installed_noise):
    noise, _, nodes, _ = installed_noise
    nodes["items"] = []
    with pytest.raises(RuntimeError, match="no Linux nodes"):
        noise._wait_for_daemon_coverage(timeout=0)


def test_new_install_uses_the_shared_values_and_verifies_coverage(installed_noise):
    noise, *_ = installed_noise
    noise.kubectl.exec_command.return_value = ""
    noise.kubectl.exec_command.side_effect = None
    noise._ensure_chaos_mesh_installed()
    assert noise._chaos_mesh_ready
    commands = [c.args[0] for c in noise.kubectl.exec_command.call_args_list]
    install = next(c for c in commands if c.startswith("helm upgrade --install"))
    assert "chaos-mesh-values.yaml" in install
    assert "--version 2.8.0" in install


@pytest.mark.parametrize("existing_release", [True, False])
def test_legacy_workload_injector_checks_daemon_coverage(existing_release):
    from sregym.generators.noise.impl import stress_injector

    with (
        patch.object(stress_injector, "KubeCtl") as kubectl,
        patch.object(stress_injector, "Helm") as helm,
        patch.object(stress_injector, "get_noise_manager") as get_manager,
    ):
        kubectl.return_value.get_container_runtime.return_value = "containerd"
        kubectl.return_value.exec_command.return_value = ""
        helm.exists_release.return_value = existing_release
        stress_injector.ChaosInjector("application")
        get_manager.return_value.wait_for_chaos_mesh_ready.assert_called_once_with()
        assert helm.install.called is not existing_release


@pytest.fixture
def noise(monkeypatch):
    monkeypatch.setattr(manager.NoiseManager, "_instance", None)
    monkeypatch.setattr(manager, "KubeCtl", Mock())
    noise = manager.NoiseManager()
    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", lambda: setattr(noise, "_chaos_mesh_ready", True))
    noise.kubectl.exec_command.return_value = ""
    return noise


def test_stop_finishes_pending_apply_before_cleanup(noise):
    apply_entered = threading.Event()
    release_apply = threading.Event()
    actions = []

    def execute(command):
        if command.startswith("kubectl apply "):
            apply_entered.set()
            assert release_apply.wait(3)
            actions.append("apply")
        elif command.startswith("kubectl delete "):
            actions.append("delete")
        return ""

    noise.kubectl.exec_command.side_effect = execute
    noise.set_problem_context({"namespace": "application"})
    noise.start()
    worker = noise._background_thread
    with concurrent.futures.ThreadPoolExecutor() as pool:
        try:
            assert apply_entered.wait(1)
            stopped = pool.submit(noise.stop)
            assert noise._stop_event.wait(1)
            assert not stopped.done()
            assert actions == []
            release_apply.set()
            stopped.result(timeout=2)
        finally:
            release_apply.set()
            noise.stop()

    assert not worker.is_alive()
    assert actions == ["apply", "delete"]
    assert noise.active_experiments == []
    assert noise._background_thread is None


def test_timed_out_stop_retains_worker_until_cleanup(noise, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def inject():
        entered.set()
        release.wait(10)

    monkeypatch.setattr(manager, "STOP_TIMEOUT", 0.02, raising=False)
    monkeypatch.setattr(noise, "_maybe_inject", inject)
    cleanup = Mock()
    monkeypatch.setattr(noise, "_cleanup_experiments", cleanup)
    noise.start()
    worker = noise._background_thread
    try:
        assert entered.wait(1)
        with pytest.raises(TimeoutError, match="Noise injection did not stop"):
            noise.stop()
        assert noise._background_thread is worker
        assert worker.is_alive()
        cleanup.assert_not_called()
        with pytest.raises(RuntimeError, match="must be stopped"):
            noise.start()
    finally:
        release.set()
        worker.join(timeout=6)

    # Finishing the worker alone does not clean up the experiments it created.
    with pytest.raises(RuntimeError, match="must be stopped"):
        noise.start()
    noise.stop()
    cleanup.assert_called_once()
    assert noise._background_thread is None
    noise.start()
    try:
        assert noise._background_thread is not worker
    finally:
        noise.stop()


def test_stop_wakes_idle_worker_and_can_be_repeated(noise, monkeypatch):
    waiting = threading.Event()
    wait = noise._stop_event.wait

    def idle_wait(timeout):
        waiting.set()
        return wait(timeout)

    monkeypatch.setattr(noise._stop_event, "wait", idle_wait)
    monkeypatch.setattr(manager, "STOP_TIMEOUT", 0.2)
    noise.start()
    worker = noise._background_thread
    try:
        assert waiting.wait(1)
    finally:
        noise.stop()
    noise.stop()
    assert not worker.is_alive()
    assert noise._background_thread is None


def test_restart_waits_for_stop_cleanup(noise, monkeypatch):
    cleaning = threading.Event()
    release = threading.Event()
    restarting = threading.Event()
    actions = []

    def cleanup():
        cleaning.set()
        assert release.wait(3)
        actions.append("cleanup")

    def restart():
        restarting.set()
        noise.start()
        actions.append("restart")

    monkeypatch.setattr(noise, "_cleanup_experiments", cleanup)
    noise.start()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        try:
            stopped = pool.submit(noise.stop)
            assert cleaning.wait(1)
            started = pool.submit(restart)
            assert restarting.wait(1)
            with pytest.raises(concurrent.futures.TimeoutError):
                started.result(timeout=0.05)
        finally:
            release.set()
        stopped.result(timeout=2)
        started.result(timeout=2)
    try:
        assert actions == ["cleanup", "restart"]
        assert noise.running
    finally:
        noise.stop()


def test_stop_during_slow_start_prevents_late_worker(noise, monkeypatch):
    installing = threading.Event()
    release = threading.Event()

    def install():
        installing.set()
        assert release.wait(3)
        noise._chaos_mesh_ready = True

    monkeypatch.setattr(noise, "_ensure_chaos_mesh_installed", install)
    monkeypatch.setattr(manager, "STOP_TIMEOUT", 0.02)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        started = pool.submit(noise.start)
        try:
            assert installing.wait(1)
            stopped = pool.submit(noise.stop)
            with pytest.raises(TimeoutError, match="Noise lifecycle operation"):
                stopped.result(timeout=1)
        finally:
            release.set()
        started.result(timeout=1)

    assert noise._background_thread is None
    assert not noise.running
    noise.stop()
