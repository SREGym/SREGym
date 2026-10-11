import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
    KubeletEvictionThresholdMisconfigMitigationOracle,
)
from sregym.conductor.problems.file_descriptor_exhaustion import FileDescriptorExhaustion
from sregym.conductor.problems.stale_hostaliases_dns_poisoning_astronomy_shop import (
    StaleHostAliasesDNSPoisoningAstronomyShop,
)
from sregym.service.verifier_runtime import VerifierRuntime
from sregym.service.verifier_state import restore_oracle, save_oracle_snapshot, snapshot_oracle


@pytest.mark.parametrize("saved", [False, True])
def test_cart_snapshot_drains_real_owner_before_copying_acknowledged_history(monkeypatch, tmp_path, saved):
    problem = object.__new__(StaleHostAliasesDNSPoisoningAstronomyShop)
    problem._stop = threading.Event()
    problem.expected_carts = {}

    def traffic():
        problem._stop.wait()
        problem.expected_carts["customer"] = {"product": 2}

    problem._thread = threading.Thread(target=traffic)
    runtime = VerifierRuntime()
    runtime.image = "unit-only"
    runtime.kubeconfig = {}
    monkeypatch.setattr("sregym.service.verifier_runtime.verifier_connection", lambda *a, **k: ({}, "unit-only"))
    captured = []
    monkeypatch.setattr(runtime, "evaluate_snapshot", lambda payload, *a, **k: captured.append(restore_oracle(payload)))
    try:
        problem._thread.start()
        oracle = SimpleNamespace(problem=problem)
        if saved:
            path = tmp_path / "private-baseline"
            save_oracle_snapshot(oracle, path)
            captured.append(restore_oracle(path.read_bytes()))
        else:
            runtime.evaluate(oracle)
        assert problem._thread is None
        assert captured[0].problem.expected_carts == {"customer": {"product": 2}}
    finally:
        problem.stop_traffic()


def test_cart_diagnosis_does_not_stop_owner_traffic(monkeypatch):
    problem = object.__new__(StaleHostAliasesDNSPoisoningAstronomyShop)
    problem._stop = threading.Event()
    problem._thread = threading.Thread(target=problem._stop.wait)
    runtime = VerifierRuntime()
    runtime.image = "unit-only"
    runtime.kubeconfig = {}
    monkeypatch.setattr("sregym.service.verifier_runtime.verifier_connection", lambda *a, **k: ({}, "unit-only"))
    captured = []
    monkeypatch.setattr(runtime, "evaluate_snapshot", lambda payload, *a, **k: captured.append(restore_oracle(payload)))
    try:
        problem._thread.start()
        runtime.evaluate(SimpleNamespace(problem=problem), "diagnosis")
        assert problem._thread.is_alive()
        assert not problem._stop.is_set()
        assert captured[0].problem._thread is None
    finally:
        problem.stop_traffic()


def test_fd_snapshot_omits_only_live_runner_thread():
    problem = object.__new__(FileDescriptorExhaustion)
    problem.flooder_thread = threading.Thread(target=lambda: None)
    problem.pf_process = object.__new__(subprocess.Popen)
    problem.pf_process._child_created = False
    problem.stop_flooder = threading.Event()
    problem.forced_ulimit = 1024
    problem.faulty_service = "frontend"
    snapshot, _ = snapshot_oracle(problem, Path(__file__).resolve().parents[2])
    assert snapshot
    assert problem.flooder_thread is not None
    assert problem.pf_process is not None
    assert FileDescriptorExhaustion.verifier_excluded_fields == ("flooder_thread", "pf_process")


def test_unknown_thread_is_still_a_snapshot_error():
    problem = object.__new__(FileDescriptorExhaustion)
    problem.unregistered_thread = threading.Thread(target=lambda: None)
    with pytest.raises(TypeError, match="thread/process"):
        snapshot_oracle(problem, Path(__file__).resolve().parents[2])


@pytest.mark.parametrize("config", ["", "\n  "])
def test_empty_container_kubelet_probe_fails_closed(monkeypatch, config):
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    oracle = object.__new__(KubeletEvictionThresholdMisconfigMitigationOracle)
    oracle.problem = SimpleNamespace(kubectl=Mock(run_node_script_pod=Mock(return_value=config)))
    injector = Mock()
    with pytest.raises(RuntimeError, match="no kubelet configuration"):
        oracle._read_kubelet_config(injector, "node-1")
    injector._docker_exec.assert_not_called()
    injector._node_exec.assert_not_called()


def test_container_kubelet_probe_uses_private_trusted_helper(monkeypatch):
    monkeypatch.setenv("SREGYM_VERIFIER_CONTAINER", "1")
    oracle = object.__new__(KubeletEvictionThresholdMisconfigMitigationOracle)
    oracle.problem = SimpleNamespace(
        kubectl=Mock(run_node_script_pod=Mock(return_value='kind: KubeletConfiguration\nnodefs.available: "85%"\n'))
    )
    injector = Mock()
    assert oracle._read_kubelet_config(injector, "node-1") == 'nodefs.available: "85%"'
    oracle.problem.kubectl.run_node_script_pod.assert_called_once_with(
        node_name="node-1",
        namespace="platform-maintenance",
        script="cat /host/var/lib/kubelet/config.yaml",
        name_prefix="kubelet-check",
    )
    injector._docker_exec.assert_not_called()
    injector._node_exec.assert_not_called()


@pytest.mark.parametrize("kind", [False, True])
def test_trusted_non_container_probe_preserves_existing_route(monkeypatch, kind):
    monkeypatch.delenv("SREGYM_VERIFIER_CONTAINER", raising=False)
    oracle = object.__new__(KubeletEvictionThresholdMisconfigMitigationOracle)
    injector = Mock()
    injector._check_is_kind.return_value = kind
    oracle._read_kubelet_config(injector, "node-1")
    selected = injector._docker_exec if kind else injector._node_exec
    selected.assert_called_once_with("node-1", "grep 'nodefs.available' /var/lib/kubelet/config.yaml || true")
