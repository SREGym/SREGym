from unittest.mock import Mock, patch

import pytest
import yaml

from sregym.paths import MCP_SERVER_K8S
from sregym.service.kubectl import WAIT_FOR_POD_READY_TIMEOUT
from sregym.service.kubernetes_access_policy import restricted_cluster_role
from sregym.service.mcp_server import MCPServer


@pytest.fixture
def rendered_manifests():
    # Use the real manifest resources. Kustomize adds labels and image names,
    # but does not change the RBAC rules under test.
    manifest = yaml.safe_load((MCP_SERVER_K8S / "kustomization.yaml").read_text())
    return yaml.safe_dump_all([yaml.safe_load((MCP_SERVER_K8S / path).read_text()) for path in manifest["resources"]])


def verbs(role, resource, group="crd.projectcalico.org"):
    return {
        verb
        for rule in role["rules"]
        if group in rule["apiGroups"] and resource in rule["resources"]
        for verb in rule["verbs"]
    }


@pytest.mark.parametrize("filtered", [True, False])
@pytest.mark.parametrize("already_running", [True, False])
def test_deploy_applies_final_permissions_before_exposing_mcp(rendered_manifests, filtered, already_running):
    with patch("sregym.service.mcp_server.KubeCtl"):
        server = MCPServer(restrict_network_access=filtered)
    events = Mock()
    events.attach_mock(server.kubectl.exec_command_checked, "command")
    server.kubectl.exec_command_checked.side_effect = lambda command, **kwargs: (
        rendered_manifests if command.startswith("kubectl kustomize ") else ""
    )
    server._is_running = Mock(return_value=already_running)
    server._is_port_forward_healthy = Mock(return_value=False)
    server.start_port_forward = Mock()
    events.attach_mock(server.start_port_forward, "expose")

    server.deploy()

    calls = server.kubectl.exec_command_checked.call_args_list
    applies = [call for call in calls if call.args[0].startswith("kubectl apply")]
    assert len(applies) == 1
    assert applies[0].args == ("kubectl apply -f -",)
    resources = list(yaml.safe_load_all(applies[0].kwargs["input_data"]))
    role = next(body for body in resources if body["kind"] == "ClusterRole")
    for resource in ("felixconfigurations", "networkpolicies", "globalnetworkpolicies"):
        assert ("create" in verbs(role, resource)) is (not filtered or resource != "felixconfigurations")
        assert {"get", "list", "watch"} <= verbs(role, resource)
    assert "patch" in verbs(role, "bgppeers")
    assert "patch" in verbs(role, "networkpolicies", "networking.k8s.io")
    assert "patch" in verbs(role, "configmaps", "")

    expected = str(filtered).lower()
    if already_running:
        assert {body["kind"] for body in resources} == {"ClusterRole", "ClusterRoleBinding"}
        env_command = next(call.args[0] for call in calls if "set env" in call.args[0])
        assert f"RESTRICT_NETWORK_ACCESS={expected}" in env_command
        assert f"BLOCK_WORKLOAD_CREATION={expected}" in env_command
    else:
        deployment = next(body for body in resources if body["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        assert env["RESTRICT_NETWORK_ACCESS"] == expected
        assert env["BLOCK_WORKLOAD_CREATION"] == expected
        assert not any("set env" in call.args[0] for call in calls)

    assert "rollout status" in events.mock_calls[-2].args[0]
    rollout = next(call for call in calls if "rollout status" in call.args[0])
    assert f"--timeout={WAIT_FOR_POD_READY_TIMEOUT}s" in rollout.args[0]
    assert rollout.kwargs["timeout"] == WAIT_FOR_POD_READY_TIMEOUT + 10
    assert events.mock_calls[-1] == ("expose", (), {})


def test_restricted_role_does_not_change_the_open_manifest(rendered_manifests):
    original = next(body for body in yaml.safe_load_all(rendered_manifests) if body["kind"] == "ClusterRole")
    restricted = restricted_cluster_role(original)
    assert "create" in verbs(original, "globalnetworkpolicies")
    assert "create" in verbs(restricted, "globalnetworkpolicies")
    assert "create" not in verbs(restricted, "felixconfigurations")
    assert restricted_cluster_role(restricted) == restricted


@pytest.mark.parametrize("failed_command", ["kustomize", "apply", "rollout"])
def test_failed_mcp_configuration_is_not_exposed(rendered_manifests, failed_command):
    with patch("sregym.service.mcp_server.KubeCtl"):
        server = MCPServer(restrict_network_access=True)
    server._is_running = Mock(return_value=False)
    server.start_port_forward = Mock()

    def execute(command, **kwargs):
        if failed_command in command:
            raise RuntimeError("configuration failed")
        return rendered_manifests if command.startswith("kubectl kustomize ") else ""

    server.kubectl.exec_command_checked.side_effect = execute
    with pytest.raises(RuntimeError, match="configuration failed"):
        server.deploy()
    server.start_port_forward.assert_not_called()


def test_mcp_rollout_retries_once_and_logs_cluster_diagnostics(monkeypatch):
    server = object.__new__(MCPServer)
    server.namespace = "sregym"
    server.service_name = "mcp-server"
    server.kubectl = Mock()
    rollouts = 0

    def execute(command, **kwargs):
        nonlocal rollouts
        if "rollout status" in command:
            rollouts += 1
            if rollouts == 1:
                raise RuntimeError("rollout timeout")
        else:
            assert kwargs["timeout"] == 10
        return "pending pod" if "get pods" in command else "image pull in progress"

    server.kubectl.exec_command_checked.side_effect = execute
    monkeypatch.setattr("sregym.service.mcp_server.time.sleep", lambda _: None)

    server._wait_for_rollout()

    assert rollouts == 2
    assert server.kubectl.exec_command_checked.call_count == 4
    server.kubectl.exec_command.assert_not_called()


def test_mcp_diagnostic_failures_remain_bounded_and_do_not_hide_rollout_error(monkeypatch):
    server = object.__new__(MCPServer)
    server.namespace = "sregym"
    server.service_name = "mcp-server"
    server.kubectl = Mock()
    server.kubectl.exec_command_checked.side_effect = RuntimeError("command deadline")
    monkeypatch.setattr("sregym.service.mcp_server.MCP_ROLLOUT_ATTEMPTS", 1)

    with pytest.raises(RuntimeError, match="MCP rollout did not become ready.*command deadline"):
        server._wait_for_rollout()

    diagnostics = server.kubectl.exec_command_checked.call_args_list[1:]
    assert len(diagnostics) == 2
    assert all(call.kwargs["timeout"] == 10 for call in diagnostics)
