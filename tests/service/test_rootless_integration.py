"""Opt-in checks using separate workload and trusted Docker engines."""

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.base import Oracle
from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.docker_runtime import docker_command, trusted_docker_host, validate_rootless_boundary
from sregym.service.internet_policy import InternetPolicy
from sregym.service.verifier_runtime import VerifierRuntime
from sregym.service.verifier_state import snapshot_oracle

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def environment():
    path = os.environ.get("SREGYM_ROOTLESS_TEST_KUBECONFIG")
    if not path or os.environ.get("SREGYM_ROOTLESS_WORKLOAD") != "1":
        pytest.skip("Requires an explicitly selected disposable rootless workload cluster")
    audit = validate_rootless_boundary()
    config = Path(path)
    verifier = VerifierRuntime(kubeconfig_path=config, timeout_seconds=60)
    verifier.prepare()
    runner = ContainerRunner(
        ContainerConfig(
            kubeconfig_path=config,
            codex_auth="none",
            forward_host_credentials=False,
            internet_policy=InternetPolicy.from_mode("open"),
        )
    )
    runner.ensure_image_exists()
    yield config, audit, verifier, runner.config.image
    verifier.cancel()
    runner.close()


def test_real_agent_uses_volumes_and_cannot_replace_run_metadata(environment, tmp_path):
    config, audit, _, image = environment
    metadata = tmp_path / "result.json"
    metadata.write_text("trusted verdict")
    inputs = tmp_path / "private"
    inputs.mkdir(mode=0o700)
    selected = inputs / "config"
    selected.write_bytes(config.read_bytes())
    selected.chmod(0o600)
    (inputs / "private-baseline").write_text("runner-only baseline")
    logs = tmp_path / "agent"
    runner = ContainerRunner(
        ContainerConfig(
            image=image,
            kubeconfig_path=selected,
            logs_path=logs,
            codex_auth="none",
            forward_host_credentials=False,
            internet_policy=InternetPolicy.from_mode("open"),
        )
    )
    code = """import json,time
from pathlib import Path
assert Path('/root/.kube/config').is_file()
assert not Path('/root/.kube/private-baseline').exists()
assert not Path('/var/run/docker.sock').exists()
assert not Path('/run/user/20040').exists()
assert not Path('/users/skizzy/SREGym').exists()
Path('/logs/result.json').write_text('forged result')
Path('/logs/../result.json').write_text('forged parent result')
Path('/logs/driver.log').write_text('agent output')
print(json.dumps({'uid_map':Path('/proc/self/uid_map').read_text(),
 'cpu':Path('/sys/fs/cgroup/cpu.max').read_text(),
 'memory':Path('/sys/fs/cgroup/memory.max').read_text()}),flush=True)
time.sleep(5)
"""
    request = ExecInput(command=f"python3 -c {shlex.quote(code)}", label="rootless-boundary")
    process = None
    try:
        process = runner.run_async(request)
        # Docker creates asynchronously; inspect after the command prints its
        # in-container observations while it is deliberately still alive.
        observations = json.loads(process.stdout.readline())
        uid_map = observations["uid_map"].splitlines()[0].split()
        assert int(uid_map[1]) == audit["workload_uid"]
        assert observations["cpu"].strip() == "400000 100000"
        assert observations["memory"].strip() == str(8 * 1024**3)
        info = json.loads(
            subprocess.run(
                docker_command("inspect", request.container_name),
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
        )[0]
        assert info["Mounts"] and all(mount["Type"] == "volume" for mount in info["Mounts"])
        assert info["HostConfig"]["Privileged"] is False
        assert process.wait(timeout=30) == 0
    finally:
        if process is not None and process.poll() is None:
            runner.stop_container(request.container_name)
            process.wait(timeout=15)
        runner.close()
    assert metadata.read_text() == "trusted verdict"
    assert (logs / "driver.log").read_text() == "agent output"
    assert (logs / "result.json").read_text() == "forged result"
    assert not (tmp_path / "private-baseline").exists()


def test_separate_verifier_preserves_baseline_and_accepts_agent_interface_repair(environment, tmp_path):
    config, _, verifier, image = environment
    namespace = "rootless-recovery-" + uuid.uuid4().hex[:8]

    def kubectl(*args):
        return subprocess.run(
            ["kubectl", "--kubeconfig", str(config), *args], capture_output=True, text=True, check=True, timeout=120
        )

    class ReplicaOracle(Oracle):
        def evaluate(self):
            import json
            import subprocess

            deployment = json.loads(
                subprocess.run(
                    ["kubectl", "get", "deployment", "web", "-n", self.problem.namespace, "-o", "json"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                ).stdout
            )
            return {
                "success": deployment["spec"]["replicas"] == self.baseline
                and deployment.get("status", {}).get("availableReplicas", 0) == self.baseline
            }

    oracle = ReplicaOracle(SimpleNamespace(namespace=namespace))
    oracle.baseline = 2

    def grade():
        payload, resources = snapshot_oracle(oracle, Path(__file__).resolve().parents[2])
        return verifier.evaluate_snapshot(payload, resources)["success"]

    runner = ContainerRunner(
        ContainerConfig(
            image=image,
            kubeconfig_path=config,
            logs_path=tmp_path / "agent",
            codex_auth="none",
            forward_host_credentials=False,
            internet_policy=InternetPolicy.from_mode("open"),
        )
    )
    try:
        kubectl("create", "namespace", namespace)
        kubectl("create", "deployment", "web", "-n", namespace, "--image=nginx:1.27.5-alpine", "--replicas=2")
        kubectl("rollout", "status", "deployment/web", "-n", namespace, "--timeout=90s")
        assert grade() is True
        kubectl("scale", "deployment/web", "-n", namespace, "--replicas=0")
        assert grade() is False
        result = runner.run_sync(
            ExecInput(
                command=f"kubectl scale deployment/web -n {namespace} --replicas=2 && "
                f"kubectl rollout status deployment/web -n {namespace} --timeout=90s",
                label="rootless-reference-repair",
                timeout=120,
            )
        )
        assert result.returncode == 0, result.stderr
        assert grade() is True
        assert oracle.baseline == 2
        assert verifier.docker_command("test")[:3] == ["docker", "--host", trusted_docker_host()]
    finally:
        runner.close()
        kubectl("delete", "namespace", namespace, "--wait=false")


def test_rootless_kubernetes_distinguishes_real_oom_from_other_exit_137(environment):
    config, audit, _, _ = environment
    namespace = "rootless-oom-" + uuid.uuid4().hex[:8]
    node = next(name for name in reversed(audit["nodes"]) if "worker" in name)

    def kubectl(*args, **kwargs):
        return subprocess.run(
            ["kubectl", "--kubeconfig", str(config), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
            **kwargs,
        )

    cases = {f"oom-{index}": ("oom", 0) for index in range(8)}
    cases.update({"oom-delayed": ("oom", 3), "sigkill": ("sigkill", 1), "exit-137": ("exit", 1)})
    resources = []
    for name, (kind, delay) in cases.items():
        action = {
            "oom": "data=bytearray(128*1024*1024)",
            # PID namespace init ignores SIGKILL sent from its own namespace.
            # Kill this control from the logical node's ancestor namespace.
            "sigkill": "time.sleep(3600)",
            "exit": "sys.exit(137)",
        }[kind]
        resources.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "nodeName": node,
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": name,
                            "image": "python:3.12.3-slim",
                            "command": [
                                "python",
                                "-c",
                                f"import os,signal,sys,time; time.sleep({delay}); {action}",
                            ],
                            "resources": {"requests": {"memory": "32Mi"}, "limits": {"memory": "64Mi"}},
                        }
                    ],
                },
            }
        )
    observed = {}
    try:
        kubectl("create", "namespace", namespace)
        kubectl("apply", "-f", "-", input=json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}))
        kubectl("wait", "--for=condition=Ready", "pod/sigkill", "-n", namespace, "--timeout=60s")
        control = json.loads(kubectl("get", "pod/sigkill", "-n", namespace, "-o", "json").stdout)
        container_id = control["status"]["containerStatuses"][0]["containerID"].removeprefix("containerd://")
        assert len(container_id) == 64 and all(character in "0123456789abcdef" for character in container_id)
        subprocess.run(
            docker_command(
                "exec",
                node,
                "ctr",
                "--namespace=k8s.io",
                "tasks",
                "kill",
                "--signal",
                "SIGKILL",
                container_id,
                host=os.environ["DOCKER_HOST"],
            ),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            pods = json.loads(kubectl("get", "pods", "-n", namespace, "-o", "json").stdout)["items"]
            observed = {
                pod["metadata"]["name"]: (pod.get("status", {}).get("containerStatuses") or [{}])[0]
                .get("state", {})
                .get("terminated", {})
                for pod in pods
            }
            if len(observed) == len(cases) and all(
                observed[name].get("exitCode") == 137
                and observed[name].get("reason") == ("OOMKilled" if kind == "oom" else "Error")
                for name, (kind, _) in cases.items()
            ):
                break
            time.sleep(1)
        assert len(observed) == len(cases), observed
        for name, (kind, _) in cases.items():
            assert observed[name].get("exitCode") == 137, observed
            assert observed[name].get("reason") == ("OOMKilled" if kind == "oom" else "Error"), observed
    finally:
        kubectl("delete", "namespace", namespace, "--wait=false")


def test_filtered_rootless_agent_can_reach_allowed_runner_and_records_denial(environment, tmp_path):
    _, _, _, image = environment

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"runner reached")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((os.environ["SREGYM_RUNNER_ADDRESS"], 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    runner = ContainerRunner(
        ContainerConfig(
            image=image,
            logs_path=tmp_path / "agent",
            codex_auth="none",
            forward_host_credentials=False,
            env_vars={"API_PORT": str(port)},
            internet_policy=InternetPolicy.from_mode("filtered"),
        )
    )
    try:
        code = f"""import requests
assert requests.get('http://host.docker.internal:{port}/',timeout=15).text == 'runner reached'
blocked = requests.get('http://example.com/',timeout=15)
assert blocked.status_code == 451, blocked.status_code
try:
 requests.get('https://example.com/',timeout=15)
except requests.exceptions.ProxyError as exc:
 assert '451' in str(exc), str(exc)
else:
 raise AssertionError('HTTPS CONNECT unexpectedly allowed')
"""
        result = runner.run_sync(
            ExecInput(command=f"python3 -c {shlex.quote(code)}", label="rootless-filtered", timeout=90)
        )
        assert result.returncode == 0, result.stderr or result.stdout
        records = runner.blocked_request_records()
        assert any(record["host"] == "example.com" for record in records)
    finally:
        runner.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
