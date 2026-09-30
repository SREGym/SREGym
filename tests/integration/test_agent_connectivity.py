"""Read-only container-to-Kubernetes checks; requires KIND and the agent image.

Run explicitly with: uv run pytest tests/integration/test_agent_connectivity.py -m integration -v
No agent installation or model API request is made.
"""

import json
import shlex
import socket
from pathlib import Path

import pytest

from sregym.service.container_runner import (
    ContainerConfig,
    ContainerRunner,
    ExecInput,
    get_container_host_bind_address,
)
from sregym.service.internet_policy import InternetPolicy
from sregym.service.k8s_proxy import KubernetesAPIProxy
from sregym.service.mcp_server import MCPServer

pytestmark = pytest.mark.integration


@pytest.fixture
def mcp_forward(monkeypatch):
    server = MCPServer()
    # Use our own free port, never the active campaign's port-forward.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server.port = sock.getsockname()[1]
    monkeypatch.setenv("MCP_SERVER_PORT", str(server.port))
    try:
        server.start_port_forward()
        assert server._is_port_forward_healthy()
        yield server
    finally:
        server.stop_port_forward()


@pytest.mark.parametrize("mode", ["open", "filtered"])
def test_agent_can_query_cluster_and_mcp_tools(mode, monkeypatch, tmp_path, mcp_forward):
    proxy = KubernetesAPIProxy(listen_host=get_container_host_bind_address(), listen_port=16443)
    runner = None
    try:
        proxy.start()
        kubeconfig = Path(proxy.generate_agent_kubeconfig())
        runner = ContainerRunner(
            ContainerConfig(
                kubeconfig_path=kubeconfig,
                internet_policy=InternetPolicy.from_mode(mode),
                memory="1g",
                cpus=1,
            )
        )
        # This test needs only the ephemeral proxy token, not provider or CLI credentials.
        monkeypatch.setattr(runner, "API_KEY_VARS", [])
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = runner.run_sync(
            ExecInput(
                command="kubectl --request-timeout=20s get nodes -o json",
                timeout=120,
                label="connectivity-check",
            )
        )
        assert result.returncode == 0, result.stderr
        nodes = json.loads(result.stdout)["items"]
        assert nodes, "Proxy returned no cluster nodes"
        assert all(node["status"]["nodeInfo"]["architecture"] in {"arm64", "amd64"} for node in nodes)
        script = """
import ast, asyncio, json, os
from mcp import ClientSession
from mcp.client.sse import sse_client

async def check():
    found = {}
    for service in ('kubectl', 'prometheus', 'loki', 'jaeger'):
        url = os.environ['MCP_SERVER_URL'] + '/' + service + '/sse'
        async with sse_client(url, timeout=20) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                response = await session.list_tools()
                assert response.tools, service
                found[service] = [tool.name for tool in response.tools]
                calls = {
                    'kubectl': ('exec_kubectl_cmd_safely', {'cmd': 'kubectl get nodes -o name'}),
                    'prometheus': ('get_metrics', {'query': 'up'}),
                    'loki': ('get_labels', {}),
                    'jaeger': ('get_services', {}),
                }
                tool, arguments = calls[service]
                result = await session.call_tool(tool, arguments)
                assert not result.isError, result
                text = '\\n'.join(item.text for item in result.content if hasattr(item, 'text'))
                if service == 'kubectl':
                    assert 'node/' in text, text
                elif service == 'prometheus':
                    assert ast.literal_eval(text)['result'], text
                elif service == 'loki':
                    assert 'namespace' in text, text
                else:
                    assert ast.literal_eval(text), text
    print(json.dumps(found))

asyncio.run(check())
"""
        result = runner.run_sync(
            ExecInput(command="python3 -c " + shlex.quote(script), timeout=120, label="mcp-connectivity-check")
        )
        assert result.returncode == 0, result.stderr
        assert set(json.loads(result.stdout)) == {"kubectl", "prometheus", "loki", "jaeger"}
    finally:
        if runner is not None:
            runner.cleanup_egress_proxy()
            runner.cleanup_credential_tmps()
        proxy.stop()
