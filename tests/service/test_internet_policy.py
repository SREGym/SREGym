import asyncio
import stat
from pathlib import Path

import pytest
import yaml

from clients.claudecode.claudecode_agent import ClaudeCodeAgent
from clients.copilot.copilot_agent import CopilotCliAgent
from clients.geminicli.geminicli_agent import GeminiCliAgent
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import AgentRegistration
from sregym.conductor.conductor import ConductorConfig
from sregym.service.container_runner import (
    PROXY_BUNDLE_CONTAINER_PATH,
    PROXY_CA_CONTAINER_PATH,
    ContainerConfig,
    ContainerRunner,
)
from sregym.service.internet_policy import InternetPolicy, blocked_github_owner


@pytest.mark.parametrize(
    ("host", "target", "body", "expected"),
    [
        ("github.com", "/SREGym/SREGym", None, "SREGym"),
        ("github.com", "/orgs/sregym/repositories", None, "SREGym"),
        ("github.com", "/search?q=org%253ASREGym", None, "SREGym"),
        ("api.github.com", "/repos/SREGym/SREGym", None, "SREGym"),
        ("api.github.com", "/graphql", b'{"variables":{"owner":"SREGym"}}', "SREGym"),
        ("api.github.com", "/graphql", b'{"variables":{"owner":"S\\u0052EGym"}}', "SREGym"),
        ("api.github.com", "/repositories/986527564/contents/README.md", None, "repository:986527564"),
        ("raw.githubusercontent.com", "/SREGym/SREGym/main/README.md", None, "SREGym"),
        ("codeload.github.com", "/SREGym/SREGym/tar.gz/main", None, "SREGym"),
    ],
)
def test_blocks_configured_github_owner(host, target, body, expected):
    assert blocked_github_owner(host, target, body) == expected


@pytest.mark.parametrize(
    ("host", "target"),
    [
        ("github.com", "/kubernetes/kubernetes"),
        ("api.github.com", "/repos/kubernetes/kubernetes"),
        ("raw.githubusercontent.com", "/kubernetes/kubernetes/master/README.md"),
        ("example.com", "/SREGym/SREGym"),
    ],
)
def test_allows_other_github_owners_and_hosts(host, target):
    assert blocked_github_owner(host, target, None) is None


@pytest.mark.parametrize(
    "target",
    [
        "/kubernetes/../SREGym/SREGym/blob/main/README.md",
        "/kubernetes/%2e%2e/SREGym/SREGym/blob/main/README.md",
    ],
)
def test_resolves_dot_segments_before_applying_github_policy(target):
    assert blocked_github_owner("github.com", target, None) == "SREGym"


def test_allows_unconfigured_numeric_github_repository_id():
    assert blocked_github_owner("api.github.com", "/repositories/12345/contents/README.md", None) is None


def test_filtered_runner_uses_private_network_and_proxy(tmp_path):
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode("filtered"),
            env_vars={"AGENT_API_BASE": "http://127.0.0.1:8001/v1"},
        )
    )
    runner._egress_network_name = "private-network"
    runner._egress_proxy_name = "filter-proxy"
    runner._egress_proxy_ca = tmp_path / "proxy-ca.pem"
    runner._egress_ca_bundle = tmp_path / "ca-bundle.pem"
    runner._egress_proxy_ca.touch()
    runner._egress_ca_bundle.touch()

    try:
        args = runner._build_base_docker_args()
        env_flags = runner._build_env_flags()

        assert "--network=private-network" in args
        assert "--network=host" not in args
        env = dict(item.split("=", 1) for item in env_flags[1::2])
        assert env["HTTPS_PROXY"] == "http://filter-proxy:8080"
        assert env["SSL_CERT_FILE"] == PROXY_BUNDLE_CONTAINER_PATH
        assert env["NODE_EXTRA_CA_CERTS"] == PROXY_CA_CONTAINER_PATH
        assert env["AGENT_API_BASE"] == "http://host.docker.internal:8001/v1"
    finally:
        runner.cleanup_credential_tmps()


def test_filtered_runner_prepares_proxy_audit_log():
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("filtered")))

    try:
        runner._prepare_egress_state()
        assert runner._egress_tmp_dir is not None
        blocked_log = runner._egress_tmp_dir / "blocked-requests.jsonl"

        assert stat.S_IMODE(runner._egress_tmp_dir.stat().st_mode) == 0o711
        assert stat.S_IMODE(blocked_log.stat().st_mode) == 0o622
    finally:
        runner.cleanup_egress_proxy()


def test_open_runner_keeps_existing_host_network():
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("open")))

    try:
        args = runner._build_base_docker_args()
        env_flags = runner._build_env_flags()

        assert "--network=host" in args
        env = dict(item.split("=", 1) for item in env_flags[1::2])
        assert env["AGENT_INTERNET_ACCESS"] == "open"
        assert "HTTPS_PROXY" not in env
    finally:
        runner.cleanup_credential_tmps()


def test_codex_auth_mount_does_not_expose_writable_host_directory(monkeypatch, tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text('{"tokens": {}}')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = ContainerRunner()
    args: list[str] = []

    try:
        runner._mount_codex_credentials(args)

        mount = args[args.index("-v") + 1]
        host_path, container_path, mode = mount.split(":")
        assert Path(host_path).name == "auth.json"
        assert container_path == "/root/.codex/auth.json"
        assert mode == "ro"
    finally:
        credential_tmps = list(runner._credential_tmps)
        runner.cleanup_credential_tmps()

    assert all(not Path(path).exists() for path in credential_tmps)


def test_filtered_runner_rewrites_loopback_kubeconfig(tmp_path):
    source = tmp_path / "config"
    source.write_text(
        yaml.safe_dump(
            {
                "clusters": [{"name": "proxy", "cluster": {"server": "http://127.0.0.1:16443"}}],
            }
        )
    )
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("filtered")))

    try:
        rewritten = runner._prepare_kubeconfig(source)
        data = yaml.safe_load(Path(rewritten).read_text())
        assert data["clusters"][0]["cluster"]["server"] == "http://host.docker.internal:16443"
    finally:
        runner.cleanup_credential_tmps()


def test_filtered_runner_does_not_mount_unproxied_kubeconfig(tmp_path, monkeypatch):
    source = tmp_path / "config"
    source.write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode("filtered"),
            kubeconfig_path=source,
        )
    )
    runner._egress_network_name = "private-network"
    runner._egress_proxy_ca = tmp_path / "proxy-ca.pem"
    runner._egress_ca_bundle = tmp_path / "ca-bundle.pem"
    runner._egress_proxy_ca.touch()
    runner._egress_ca_bundle.touch()

    try:
        args = runner._build_base_docker_args()
        assert "/root/.kube/config:ro" in " ".join(args)
        assert "/root/.kube/real-config:ro" not in " ".join(args)
        assert "SREGYM_REAL_KUBECONFIG" not in args
    finally:
        runner.cleanup_credential_tmps()


def test_filtered_mode_disables_claude_web_search(monkeypatch):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "filtered")
    assert "WebFetch" in ClaudeCodeAgent.allowed_tools()
    assert "WebSearch" not in ClaudeCodeAgent.allowed_tools()


def test_open_mode_keeps_claude_web_search(monkeypatch):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "open")
    assert "WebFetch" in ClaudeCodeAgent.allowed_tools()
    assert "WebSearch" in ClaudeCodeAgent.allowed_tools()


def test_filtered_mode_disables_gemini_provider_web_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "filtered")
    agent = GeminiCliAgent(logs_dir=tmp_path, model_name="gemini-2.5-pro")

    command = agent._build_command("inspect the cluster")

    assert "--exclude-tools" not in command
    settings_file = agent._prepare_filtered_settings()
    assert settings_file is not None
    try:
        assert '"google_web_search"' in settings_file.read_text()
    finally:
        settings_file.unlink()


def test_filtered_mode_disables_copilot_provider_web_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "filtered")
    agent = CopilotCliAgent(logs_dir=tmp_path, model_name="gpt-5")

    command = agent._build_command("inspect the cluster")

    assert command[-4:] == [
        "--excluded-tools",
        "web_fetch,web_search",
        "--disable-mcp-server",
        "github-mcp-server",
    ]


def test_open_mode_keeps_gemini_and_copilot_web_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "open")
    gemini = GeminiCliAgent(logs_dir=tmp_path / "gemini", model_name="gemini-2.5-pro")
    copilot = CopilotCliAgent(logs_dir=tmp_path / "copilot", model_name="gpt-5")

    assert "--exclude-tools" not in gemini._build_command("inspect the cluster")
    assert "--excluded-tools" not in copilot._build_command("inspect the cluster")
    assert "--disable-mcp-server" not in copilot._build_command("inspect the cluster")


def test_filtered_mode_rejects_non_container_agent():
    launcher = AgentLauncher()
    registration = AgentRegistration(name="local", kickoff_command="true", container_isolation=False)

    with pytest.raises(RuntimeError, match="does not use container isolation"):
        asyncio.run(launcher.ensure_started(registration))


def test_conductor_config_defaults_to_filtered_internet():
    assert ConductorConfig().internet_policy.is_filtered
