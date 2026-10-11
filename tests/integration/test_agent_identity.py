"""Audit the real public image and import client drivers without model calls."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from sregym.service.agent_visibility_policy import MCP_CONTROL_NAMESPACE
from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput, get_container_host_bind_address
from sregym.service.k8s_proxy import KubernetesAPIProxy

pytestmark = pytest.mark.integration


def test_public_image_contains_no_benchmark_name_or_private_grading_sources(monkeypatch, tmp_path):
    runner = ContainerRunner(
        ContainerConfig(
            memory="2g",
            cpus=2,
            codex_auth="none",
            forward_host_credentials=False,
        )
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script = """
import importlib
import os
from pathlib import Path

root = Path("/opt/runtime")
assert root.is_dir() and not Path("/opt/sregym").exists()
assert os.environ["RUN_ARTIFACT_ID"] == "anon_identity_test"
assert os.environ["PYTHONPATH"] == str(root)
assert not any("sregym" in key.lower() for key in os.environ)
for path in root.rglob("*"):
    assert "sregym" not in str(path).lower(), str(path)
    if path.is_file() and path.suffix in {".py", ".yaml", ".txt", ".sh"}:
        assert "sregym" not in path.read_text().lower(), str(path)
assert not (root / "incident_runtime/conductor").exists()
assert not (root / "incident_runtime/generators").exists()
assert not (root / "llm_backend/judge_bridge.py").exists()
for name in (
    "clients.codex.driver", "clients.claudecode.driver", "clients.cursor.driver",
    "clients.copilot.driver", "clients.geminicli.driver", "clients.opencode.driver",
    "clients.stratus.stratus_agent.driver.driver", "clients.demo.driver",
):
    importlib.import_module(name)
from clients.harness.problem_id import resolve_problem_id
assert resolve_problem_id() == "anon_identity_test"
print("public runtime audit and client imports passed")
"""
    try:
        runner.ensure_image_exists()
        result = runner.run_sync(
            ExecInput(
                command="python3 -c " + shlex.quote(script),
                env={"SREGYM_ARTIFACT_ID": "anon_identity_test", "AGENT_LOGS_DIR": "/logs"},
                label="identity-audit",
                timeout=120,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "public runtime audit and client imports passed" in result.stdout
    finally:
        runner.cleanup_egress_proxy()
        runner.cleanup_credential_tmps()


def test_real_agent_cannot_read_control_namespace_or_bindings_but_can_inspect_nodes(monkeypatch, tmp_path):
    upstream = os.environ.get("SREGYM_ROOTLESS_TEST_KUBECONFIG")
    if not upstream or os.environ.get("SREGYM_ROOTLESS_WORKLOAD") != "1":
        pytest.skip("Requires the explicitly configured rootless qualification cluster")
    # Verify that the control namespace actually exists before testing concealment.
    subprocess.run(
        ["kubectl", "--kubeconfig", upstream, "get", "namespace", MCP_CONTROL_NAMESPACE, "-o", "name"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    proxy = KubernetesAPIProxy(
        listen_host=get_container_host_bind_address(),
        listen_port=0,
        upstream_kubeconfig_path=upstream,
    )
    runner = None
    script = """
import json, subprocess
def query(*args, success=True):
    result = subprocess.run(['kubectl', '--request-timeout=20s', *args], capture_output=True, text=True)
    assert (result.returncode == 0) == success
    return json.loads(result.stdout) if success else None
namespaces = query('get', 'namespaces', '-o', 'json')['items']
assert not any('sregym' in json.dumps(item).lower() for item in namespaces)
bindings = query('get', 'clusterrolebindings', '-o', 'json')['items']
assert not any('sregym' in json.dumps(item).lower() for item in bindings)
query('get', 'namespace', 'sregym', success=False)
query('logs', 'mcp-server', '-n', 'sregym', success=False)
assert query('get', 'nodes', '-o', 'json')['items']
assert query('get', 'configmap', 'coredns', '-n', 'kube-system', '-o', 'json')['data']
print('control resources concealed; operational queries preserved')
"""
    try:
        proxy.start()
        proxy.listen_port = proxy.server.server_address[1]
        runner = ContainerRunner(
            ContainerConfig(
                kubeconfig_path=Path(proxy.generate_agent_kubeconfig()),
                k8s_proxy_port=proxy.listen_port,
                memory="2g",
                cpus=2,
                codex_auth="none",
                forward_host_credentials=False,
            )
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        runner.ensure_image_exists()
        result = runner.run_sync(
            ExecInput(command="python3 -c " + shlex.quote(script), timeout=120, label="control-view")
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        if runner is not None:
            runner.close()
        proxy.stop()
