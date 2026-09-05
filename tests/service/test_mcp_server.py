from unittest.mock import Mock, patch

import pytest
import yaml

from sregym.paths import MCP_SERVER_K8S
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
        assert ("create" in verbs(role, resource)) is not filtered
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
    assert events.mock_calls[-1] == ("expose", (), {})


def test_restricted_role_does_not_change_the_open_manifest(rendered_manifests):
    original = next(body for body in yaml.safe_load_all(rendered_manifests) if body["kind"] == "ClusterRole")
    restricted = restricted_cluster_role(original)
    assert "create" in verbs(original, "globalnetworkpolicies")
    assert "create" not in verbs(restricted, "globalnetworkpolicies")
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
