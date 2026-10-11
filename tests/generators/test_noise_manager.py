import concurrent.futures
import threading
from unittest.mock import Mock, patch

import pytest

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
