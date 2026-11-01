"""cluster_access: false withholds the API proxy from an agent container (no kubeconfig, no route)."""

import pytest

from sregym.agent_registry import list_agents
from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.internet_policy import InternetPolicy


def make_runner(tmp_path, **kwargs):
    kubeconfig = tmp_path / "agent.kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    kwargs.setdefault("internet_policy", InternetPolicy.from_mode("filtered"))
    runner = ContainerRunner(ContainerConfig(kubeconfig_path=kubeconfig, harden_container=False, **kwargs))
    # Stand-ins for a started egress proxy, so no Docker is needed.
    runner._egress._network_name = "private-network"
    runner._egress._proxy_name = "filter-proxy"
    runner._egress._cert_volume = "filter-certs"
    runner._egress._proxy_ca = tmp_path / "ca.pem"
    runner._egress._ca_bundle = tmp_path / "bundle.pem"
    runner._egress._proxy_ca.touch()
    runner._egress._ca_bundle.touch()
    return runner


def proxy_rules(runner):
    """Rules for the API proxy and for SREGym's MCP server (which serves kubectl tools)."""
    rules = runner._configured_egress_rules(runner._build_env_vars({}))
    return [r for r in rules if r.port in (runner.config.k8s_proxy_port, 9954)]


def docker_command(runner):
    try:
        return " ".join(runner._build_base_docker_args())
    finally:
        runner.cleanup_credential_tmps()


def test_default_keeps_kubeconfig_and_proxy_route(tmp_path):
    runner = make_runner(tmp_path)
    assert len(proxy_rules(runner)) == 2
    cmd = docker_command(runner)
    assert ":/root/.kube/config:ro" in cmd and "KUBECONFIG=/root/.kube/config" in cmd


def test_cluster_access_false_drops_kubeconfig_and_proxy_route(tmp_path):
    runner = make_runner(tmp_path, cluster_access=False)
    assert not proxy_rules(runner)
    cmd = docker_command(runner)
    assert "/root/.kube/config" not in cmd and "KUBECONFIG" not in cmd


def test_cluster_access_false_refuses_open_internet(tmp_path):
    runner = make_runner(tmp_path, cluster_access=False, internet_policy=InternetPolicy.from_mode("open"))
    with pytest.raises(ValueError, match="filtered"):
        docker_command(runner)


def test_registry_reads_cluster_access(tmp_path):
    reg = tmp_path / "agents.yaml"
    reg.write_text(
        "agents:\n  - name: a\n    kickoff_command: x\n  - name: b\n    kickoff_command: x\n    cluster_access: false\n"
    )
    agents = list_agents(reg)
    assert agents["a"].cluster_access is True
    assert agents["b"].cluster_access is False
