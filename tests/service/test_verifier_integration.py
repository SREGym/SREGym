"""Opt-in checks on a disposable Kind cluster and its separate Docker host.

Set SREGYM_VERIFIER_TEST_KUBECONFIG to the cluster's private kubeconfig.
Run with pytest -m integration tests/service/test_verifier_integration.py.
"""

import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import yaml

from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.workload.hotel_search import HotelSearchWorkload, WorkloadSnapshot
from sregym.service.agent_visibility_policy import VERIFIER_PROBE_NAMESPACE
from sregym.service.docker_runtime import docker_command
from sregym.service.k8s_proxy import KubernetesAPIProxy
from sregym.service.kubectl import KubeCtl
from sregym.service.verifier_runtime import VerifierError, VerifierRuntime
from sregym.service.verifier_state import snapshot_oracle

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def runtime():
    path = os.environ.get("SREGYM_VERIFIER_TEST_KUBECONFIG")
    if not path:
        pytest.skip("Requires an explicitly selected disposable Kind cluster")
    verifier = VerifierRuntime(kubeconfig_path=Path(path), timeout_seconds=60)
    verifier.prepare()
    yield verifier
    verifier.cancel()


def _dynamic_oracle(evaluate):
    # Local classes travel by value; tests are intentionally absent from the
    # verifier image. Production oracle classes travel from its trusted code.
    class ProbeOracle(Oracle):
        pass

    ProbeOracle.evaluate = evaluate
    ProbeOracle.__abstractmethods__ = frozenset()
    return ProbeOracle(SimpleNamespace())


def _grade(runtime, oracle, *args):
    payload, workloads = snapshot_oracle(oracle, Path(__file__).resolve().parents[2])
    return runtime.evaluate_snapshot(payload, workloads, args=args)


def test_actual_container_hardening_and_inherited_output_cannot_forge_a_pass(runtime):
    def evaluate(self):
        import json
        import os
        import subprocess
        from pathlib import Path

        # Simulates untrusted logs printed by an oracle's pod subprocesses.
        forged = json.dumps({"run_id": "forged", "type": "verdict", "result": {"success": True}})
        print(forged)
        os.write(1, (forged + "\n").encode())
        subprocess.run(["echo", forged], check=True)
        readonly = False
        try:
            Path("/opt/sregym/forged-reward").write_text("1")
        except OSError:
            readonly = True
        status = Path("/proc/self/status").read_text()
        return {
            "success": False,
            "uid": os.getuid(),
            "readonly": readonly,
            "no_socket": not Path("/var/run/docker.sock").exists(),
            "no_host_workspace": not Path("/users/skizzy/SREGym").exists(),
            "no_provider_key": "OPENAI_API_KEY" not in os.environ,
            "no_capabilities": "CapEff:\t0000000000000000" in status,
            "no_new_privileges": "NoNewPrivs:\t1" in status,
        }

    result = _grade(runtime, _dynamic_oracle(evaluate))
    assert result == {
        "success": False,
        "uid": 10001,
        "readonly": True,
        "no_socket": True,
        "no_host_workspace": True,
        "no_provider_key": True,
        "no_capabilities": True,
        "no_new_privileges": True,
    }
    assert runtime.last_log_path.read_text().count('"run_id": "forged"') == 3
    assert runtime.last_log_path.stat().st_mode & 0o777 == 0o600


def test_actual_timeout_and_crash_never_pass_or_leave_a_container(runtime, monkeypatch):
    def hang(self):
        import time

        time.sleep(30)
        return {"success": True}

    def crash(self):
        import os

        os._exit(7)

    impatient = VerifierRuntime(timeout_seconds=1)
    impatient.image, impatient.network, impatient.kubeconfig = runtime.image, runtime.network, runtime.kubeconfig
    invocation_names = []
    for subject in (impatient, runtime):
        original_command = subject.docker_command

        def track(name, command=original_command):
            invocation_names.append(name)
            return command(name)

        monkeypatch.setattr(subject, "docker_command", track)
    with pytest.raises(TimeoutError):
        _grade(impatient, _dynamic_oracle(hang))
    with pytest.raises(VerifierError, match="without a verdict"):
        _grade(runtime, _dynamic_oracle(crash))
    assert len(invocation_names) == 2
    # Other concurrent runs may legitimately use the same verifier image.
    # Cleanup must remove these invocations, without inspecting or deleting
    # another run's worker.
    for name in invocation_names:
        inspection = subprocess.run(
            docker_command("inspect", "--type", "container", name, host=runtime.docker_host),
            capture_output=True,
            timeout=30,
        )
        assert inspection.returncode != 0


def test_docker_client_death_removes_the_worker_and_never_returns_a_verdict(runtime):
    def hang(self):
        import time

        time.sleep(90)
        return {"success": True}

    isolated = VerifierRuntime(timeout_seconds=30)
    isolated.image, isolated.network, isolated.kubeconfig = runtime.image, runtime.network, runtime.kubeconfig
    completed = queue.Queue(maxsize=1)

    def grade():
        try:
            completed.put(_grade(isolated, _dynamic_oracle(hang)))
        except BaseException as exc:
            completed.put(exc)

    thread = threading.Thread(target=grade, daemon=True)
    thread.start()
    name = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            name = isolated._active_name
            if name:
                inspection = subprocess.run(
                    docker_command(
                        "inspect",
                        "--type",
                        "container",
                        "--format",
                        "{{.State.Running}}",
                        name,
                        host=isolated.docker_host,
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                if inspection.returncode == 0 and inspection.stdout.strip() == "true":
                    break
            if not thread.is_alive():
                pytest.fail(f"Worker exited before the client-death test: {completed.get_nowait()!r}")
            time.sleep(0.1)
        else:
            pytest.fail("Verifier worker never became a running Docker container")

        # The daemon owns a live container independently of the attached CLI.
        # Kill only the CLI to exercise cleanup after an already-dead client.
        process = isolated._active_process
        assert process is not None and process.poll() is None
        process.kill()
        thread.join(timeout=20)
        assert not thread.is_alive(), "Verifier did not finish after its Docker client died"
        assert isinstance(completed.get_nowait(), VerifierError)
        inspection = subprocess.run(
            docker_command("inspect", "--type", "container", name, host=isolated.docker_host),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        assert inspection.returncode != 0
        assert "No such" in inspection.stderr
        assert isolated._active_name is None
        assert isolated._active_process is None
    finally:
        isolated.cancel()
        if name:
            subprocess.run(
                docker_command("rm", "-f", name, host=isolated.docker_host),
                capture_output=True,
                check=False,
                timeout=15,
            )
        thread.join(timeout=5)


def test_actual_live_workload_observations_keep_the_host_owner(runtime):
    workload = HotelSearchWorkload("verifier-test")
    calls = []
    workload.metrics = SimpleNamespace(snapshot=lambda: {"completed": 12})
    workload.snapshot = lambda seconds: WorkloadSnapshot(12, 12, 12, seconds, 1.0, 0.1)
    workload.set_rate = lambda rate: calls.append(rate)

    def evaluate(self):
        owner = self.problem.workload
        owner.set_rate(25)
        observed = owner.snapshot(1)
        return {"success": owner.metrics.snapshot()["completed"] == observed.completed == 12}

    oracle = _dynamic_oracle(evaluate)
    oracle.problem.workload = workload
    assert _grade(runtime, oracle) == {"success": True}
    assert calls == [25]
    assert oracle.problem.workload is workload


@pytest.mark.parametrize("correct", [True, False])
def test_actual_diagnosis_scoring_runs_in_the_container_with_private_host_model_io(runtime, correct):
    class ContainerDiagnosisOracle(LLMAsAJudgeOracle):
        def evaluate(self, solution):
            import os

            assert os.environ.get("SREGYM_VERIFIER_CONTAINER") == "1"
            assert os.getuid() == 10001
            assert self.judge.api_key is None
            assert not hasattr(self.judge.backend, "api_key")
            result = super().evaluate(solution)
            result["worker_uid"] = os.getuid()
            return result

    expectation = "checkout uses port 8082 instead of the correct port 8080"
    solution = expectation if correct else "the database has run out of storage"
    oracle = ContainerDiagnosisOracle(
        SimpleNamespace(), expected=expectation, api_key="private-diagnosis-key", model_name="integration-judge"
    )
    calls = []
    question_ids = tuple(oracle.judge._all_question_ids)

    def inference(messages):
        # Only transport to a provider runs here. The existing checklist,
        # weighting and pass/fail decision execute in the actual worker.
        calls.append((os.getpid(), messages))
        content = messages[-1].content
        assert expectation in content
        assert solution in content
        answer = "Yes" if correct else "No"
        return SimpleNamespace(
            content=json.dumps(
                [
                    {"id": qid, "answer": answer, "evidence": "test response", "confidence": "High"}
                    for qid in question_ids
                ]
            )
        )

    backend = SimpleNamespace(api_key="private-provider-key", inference=inference)
    oracle.judge._backend = backend
    expected = LLMAsAJudgeOracle.evaluate(oracle, solution)
    assert expected["success"] is correct
    assert expected["accuracy"] == (100.0 if correct else 0.0)
    expected["worker_uid"] = 10001

    payload, resources = snapshot_oracle(oracle, Path(__file__).resolve().parents[2])
    assert b"private-diagnosis-key" not in payload
    assert b"private-provider-key" not in payload
    assert resources == [("model", backend)]
    assert runtime.evaluate_snapshot(payload, resources, args=(solution,)) == expected
    assert len(calls) == 2
    assert all(pid == os.getpid() for pid, _messages in calls)
    assert oracle.judge.backend is backend
    assert oracle.judge.api_key == "private-diagnosis-key"


def test_missing_telemetry_preserves_the_existing_failed_verdict(runtime, monkeypatch):
    # Other live tests may have installed Prometheus in this disposable cluster.
    # Remove the actual measuring instrument temporarily; no mocked verdict or
    # endpoint is sufficient to establish behavior inside the real worker.
    monkeypatch.setenv("KUBECONFIG", str(runtime.kubeconfig_path))
    command = ["kubectl", "--kubeconfig", str(runtime.kubeconfig_path), "-n", "observe"]
    existing = subprocess.check_output(
        [*command, "get", "deployment", "prometheus-server", "--ignore-not-found", "-o", "json"], text=True, timeout=30
    ).strip()
    deployment = json.loads(existing) if existing else None
    replicas = deployment["spec"].get("replicas", 1) if deployment else None
    try:
        if deployment:
            subprocess.run([*command, "scale", "deployment/prometheus-server", "--replicas=0"], check=True, timeout=30)
            selector = ",".join(
                f"{key}={value}" for key, value in deployment["spec"]["selector"]["matchLabels"].items()
            )
            subprocess.run(
                [*command, "wait", "--for=delete", "pod", "-l", selector, "--timeout=90s"], check=True, timeout=100
            )
        oracle = AlertOracle(SimpleNamespace(namespace="verifier-test"), buffer_seconds=0, sustained_silence_seconds=1)
        oracle._baseline_instances = {(("alertname", "chronic"), ("namespace", "verifier-test"))}
        expected = oracle.evaluate()
        assert expected["success"] is False
        assert expected["reason"] == "prometheus_unreachable"
        assert _grade(runtime, oracle) == expected
    finally:
        if deployment:
            subprocess.run(
                [*command, "scale", "deployment/prometheus-server", f"--replicas={replicas}"], check=True, timeout=30
            )
            if replicas:
                subprocess.run(
                    [*command, "rollout", "status", "deployment/prometheus-server", "--timeout=120s"],
                    check=True,
                    timeout=130,
                )


def test_node_configuration_probe_runs_through_kubernetes_without_a_docker_socket(runtime, monkeypatch):
    path = str(runtime.kubeconfig_path)
    monkeypatch.setenv("KUBECONFIG", path)
    monkeypatch.setattr("kubernetes.config.kube_config.KUBE_CONFIG_DEFAULT_LOCATION", path)
    kubectl = KubeCtl()
    node = kubectl.list_nodes().items[0].metadata.name

    def evaluate(self):
        from pathlib import Path

        from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
            KubeletEvictionThresholdMisconfigMitigationOracle,
        )

        check = KubeletEvictionThresholdMisconfigMitigationOracle(self.problem)
        check._read_kubelet_config(None, self.problem.node)
        return {"success": not Path("/var/run/docker.sock").exists()}

    oracle = _dynamic_oracle(evaluate)
    oracle.problem.kubectl, oracle.problem.node = kubectl, node
    assert _grade(runtime, oracle) == {"success": True}
    assert not kubectl.list_pods(VERIFIER_PROBE_NAMESPACE).items


@pytest.mark.parametrize("restrict_network_access", [False, True])
def test_real_native_agent_proxy_cannot_read_mutate_or_exec_into_verifier_probes(
    runtime, monkeypatch, tmp_path, restrict_network_access
):
    path = str(runtime.kubeconfig_path)
    namespace = VERIFIER_PROBE_NAMESPACE
    pod_name = "verifier-canary-" + uuid.uuid4().hex[:8]
    command = ["kubectl", "--kubeconfig", path, "-n", namespace]

    def kubectl(*args, input=None, check=True):
        return subprocess.run([*command, *args], input=input, text=True, capture_output=True, check=check, timeout=75)

    expanduser = os.path.expanduser
    monkeypatch.setattr("os.path.expanduser", lambda value: path if value == "~/.kube/config" else expanduser(value))
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    proxy = KubernetesAPIProxy(
        listen_port=port,
        restrict_network_access=restrict_network_access,
        upstream_kubeconfig_path=path,
    )
    namespace_created = False
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": namespace},
        "spec": {
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 1,
            "containers": [
                {
                    "name": "canary",
                    "image": "busybox:1.36",
                    "command": [
                        "sh",
                        "-c",
                        "echo private-probe-canary; echo trusted-state > /tmp/probe-state; "
                        "while true; do sleep 60; done",
                    ],
                }
            ],
        },
    }
    try:
        if not kubectl("get", "namespace", namespace, "--ignore-not-found", "-o", "name").stdout.strip():
            kubectl("create", "namespace", namespace)
            namespace_created = True
        kubectl("create", "-f", "-", input=json.dumps(pod))
        kubectl("wait", "--for=condition=Ready", f"pod/{pod_name}", "--timeout=60s")
        original = json.loads(kubectl("get", "pod", pod_name, "-o", "json").stdout)
        assert "private-probe-canary" in kubectl("logs", pod_name).stdout
        assert kubectl("exec", pod_name, "--", "cat", "/tmp/probe-state").stdout.strip() == "trusted-state"

        # A real existing pod prevents missing-resource 404s from making this
        # test pass when direct namespace access is accidentally permitted.
        proxy.start()
        private = yaml.safe_load(Path(proxy.generate_agent_kubeconfig()).read_text())
        from base64 import b64decode

        ca = tmp_path / "proxy-ca.pem"
        ca.write_bytes(b64decode(private["clusters"][0]["cluster"]["certificate-authority-data"]))
        ca.chmod(0o600)
        headers = {"Authorization": "Bearer " + private["users"][0]["user"]["token"]}
        url = private["clusters"][0]["cluster"]["server"]
        namespaces = requests.get(url + "/api/v1/namespaces", headers=headers, verify=str(ca), timeout=10)
        namespaces.raise_for_status()
        assert VERIFIER_PROBE_NAMESPACE not in {item["metadata"]["name"] for item in namespaces.json()["items"]}
        base = f"/api/v1/namespaces/{namespace}/pods/{pod_name}"
        attempts = [
            ("GET", "", {}),
            ("GET", "/log", {}),
            (
                "POST",
                "/exec",
                {
                    "params": [
                        ("container", "canary"),
                        ("command", "sh"),
                        ("command", "-c"),
                        ("command", "echo agent-state > /tmp/probe-state"),
                        ("stdout", "true"),
                        ("stderr", "true"),
                    ]
                },
            ),
            (
                "PATCH",
                "",
                {
                    "headers": {**headers, "Content-Type": "application/merge-patch+json"},
                    "json": {"metadata": {"annotations": {"agent-touched": "true"}}},
                },
            ),
            ("DELETE", "", {"json": {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 0}}),
        ]
        for method, suffix, kwargs in attempts:
            request_headers = kwargs.pop("headers", headers)
            response = requests.request(
                method, url + base + suffix, headers=request_headers, verify=str(ca), timeout=10, **kwargs
            )
            assert response.status_code == 404, (method, suffix, response.status_code)

        after = json.loads(kubectl("get", "pod", pod_name, "-o", "json").stdout)
        assert after["metadata"]["uid"] == original["metadata"]["uid"]
        assert not after["metadata"].get("deletionTimestamp")
        assert "agent-touched" not in after["metadata"].get("annotations", {})
        assert kubectl("exec", pod_name, "--", "cat", "/tmp/probe-state").stdout.strip() == "trusted-state"
    finally:
        proxy.stop()
        subprocess.run(
            [*command, "delete", "pod", pod_name, "--ignore-not-found", "--wait=true", "--timeout=30s"],
            capture_output=True,
            text=True,
            check=True,
            timeout=40,
        )
        if namespace_created:
            subprocess.run(
                [*command, "delete", "namespace", namespace, "--ignore-not-found", "--wait=false"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )


def test_native_conductor_preserves_baseline_and_matches_host_reference_recovery(runtime, monkeypatch):
    path = str(runtime.kubeconfig_path)
    monkeypatch.setenv("KUBECONFIG", path)
    # KubeCtl reads the Kubernetes library's default location on construction.
    monkeypatch.setattr("kubernetes.config.kube_config.KUBE_CONFIG_DEFAULT_LOCATION", path)
    namespace = "verifier-check-" + uuid.uuid4().hex[:8]

    def kubectl(*args, input=None):
        return subprocess.run(
            ["kubectl", "--kubeconfig", path, "-n", namespace, *args],
            input=input,
            text=True,
            capture_output=True,
            check=True,
            timeout=90,
        ).stdout

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "frontend", "namespace": namespace},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "frontend"}},
            "template": {
                "metadata": {"labels": {"app": "frontend"}},
                "spec": {"containers": [{"name": "frontend", "image": "nginx:1.27-alpine"}]},
            },
        },
    }

    class RecoveryProblem(Problem):
        def inject_fault(self):
            kubectl("scale", "deployment/frontend", "--replicas=0")

        def recover_fault(self):
            kubectl("apply", "-f", "-", input=json.dumps(deployment))
            kubectl("rollout", "status", "deployment/frontend", "--timeout=60s")

    kubectl("create", "namespace", namespace)
    try:
        kubectl("apply", "-f", "-", input=json.dumps(deployment))
        kubectl("rollout", "status", "deployment/frontend", "--timeout=60s")
        problem = RecoveryProblem(SimpleNamespace(namespace=namespace))
        problem.kubectl = KubeCtl()
        problem.mitigation_oracle = MitigationOracle(problem)
        conductor = Conductor.__new__(Conductor)
        conductor.config = ConductorConfig(verifier_isolation=True)
        conductor.logger = logging.getLogger("test.verifier.integration")
        conductor.execution_start_time = time.time()
        conductor.problem = problem
        conductor.stage_sequence = [{"name": "mitigation"}]
        conductor._verifier_runtime = runtime
        conductor._inject_fault()
        assert problem.mitigation_oracle.replica_count == {"frontend": 2}
        # Shorten only the test's rollout grace period; keep real acceptance.
        problem.mitigation_oracle.rollout_time = 1
        host = problem.mitigation_oracle.evaluate()
        isolated = conductor._evaluate_mitigation("")
        assert isolated == host
        assert isolated["success"] is False
        assert isolated["reason"] == "required_deployment_scaled_to_zero"

        kubectl("delete", "deployment/frontend", "--wait=true")
        isolated = conductor._evaluate_mitigation("")
        assert isolated == problem.mitigation_oracle.evaluate()
        assert isolated["reason"] == "required_deployment_missing"

        problem.recover_fault()
        isolated = conductor._evaluate_mitigation("")
        assert isolated == problem.mitigation_oracle.evaluate() == {"success": True}
        assert problem.mitigation_oracle.replica_count == {"frontend": 2}
    finally:
        kubectl("delete", "namespace", namespace, "--wait=false")
