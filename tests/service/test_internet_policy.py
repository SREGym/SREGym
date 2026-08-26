import asyncio
import base64
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
    _find_host_ca_bundle,
)
from sregym.service.internet_policy import InternetPolicy, blocked_github_owner, should_stream_response
from sregym.service.k8s_proxy import KubernetesAPIProxy, _is_workload_create_path, is_valid_bearer_token


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
        ("cdn.jsdelivr.net", "/gh/someone/SREGym@main/README.md", None, "repository:sregym"),
        ("raw.githack.com", "/someone/SREGym/main/README.md", None, "repository:sregym"),
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


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("text/event-stream", True),
        ("Text/Event-Stream; charset=utf-8", True),
        ("application/json", False),
        (None, False),
    ],
)
def test_only_event_stream_responses_are_streamed(content_type, expected):
    assert should_stream_response(content_type) is expected


def test_ignores_unrelated_query_and_json_text():
    assert blocked_github_owner("github.com", "/kubernetes/kubernetes?note=not-sregymnasium", None) is None
    assert (
        blocked_github_owner(
            "api.github.com",
            "/graphql",
            b'{"variables":{"note":"SREGym"}}',
        )
        is None
    )


def test_filtered_runner_rewrites_all_local_provider_endpoints(tmp_path):
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode("filtered"),
            env_vars={
                "AGENT_API_BASE": "http://127.0.0.1:8001/v1",
                "OPENAI_BASE_URL": "http://localhost:11434/v1",
                "ANTHROPIC_API_BASE": "http://[::1]:9000",
                "UNRELATED_VALUE": "http://127.0.0.1:9999",
            },
        )
    )
    runner._egress_network_name = "private-network"
    runner._egress_proxy_name = "filter-proxy"
    runner._egress_proxy_ca = tmp_path / "proxy-ca.pem"
    runner._egress_ca_bundle = tmp_path / "ca-bundle.pem"
    runner._egress_proxy_ca.touch()
    runner._egress_ca_bundle.touch()

    try:
        env = dict(item.split("=", 1) for item in runner._build_env_flags()[1::2])
        assert env["AGENT_API_BASE"] == "http://host.docker.internal:8001/v1"
        assert env["OPENAI_BASE_URL"] == "http://host.docker.internal:11434/v1"
        assert env["ANTHROPIC_API_BASE"] == "http://host.docker.internal:9000"
        assert env["UNRELATED_VALUE"] == "http://127.0.0.1:9999"
    finally:
        runner.cleanup_credential_tmps()


def test_host_ca_bundle_is_available():
    assert _find_host_ca_bundle().is_file()


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
        assert env["NO_PROXY"] == ""
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
                "clusters": [{"name": "proxy", "cluster": {"server": "https://127.0.0.1:16443"}}],
            }
        )
    )
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("filtered")))

    try:
        rewritten = runner._prepare_kubeconfig(source)
        data = yaml.safe_load(Path(rewritten).read_text())
        assert data["clusters"][0]["cluster"]["server"] == "https://host.docker.internal:16443"
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


@pytest.mark.parametrize(
    ("authorization", "expected"),
    [
        ("Bearer secret", True),
        ("bearer secret", True),
        ("Bearer wrong", False),
        (None, False),
        ("Basic secret", False),
    ],
)
def test_kubernetes_proxy_requires_agent_bearer_token(authorization, expected):
    assert is_valid_bearer_token(authorization, "secret") is expected


def test_kubernetes_proxy_kubeconfig_contains_private_token(tmp_path):
    proxy = KubernetesAPIProxy.__new__(KubernetesAPIProxy)
    proxy.listen_port = 16443
    proxy.listen_host = "127.0.0.1"
    proxy._agent_token = "secret"
    proxy._server_cert_pem = "-----BEGIN CERTIFICATE-----\nlocal\n-----END CERTIFICATE-----\n"
    path = tmp_path / "kubeconfig"

    proxy.generate_agent_kubeconfig(str(path))

    data = yaml.safe_load(path.read_text())
    assert data["users"][0]["user"]["token"] == "secret"
    assert data["clusters"][0]["cluster"]["server"] == "https://127.0.0.1:16443"
    assert (
        base64.b64decode(data["clusters"][0]["cluster"]["certificate-authority-data"]).decode()
        == proxy._server_cert_pem
    )
    assert "insecure-skip-tls-verify" not in data["clusters"][0]["cluster"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_kubernetes_proxy_removes_owned_kubeconfig_on_stop(tmp_path):
    proxy = KubernetesAPIProxy.__new__(KubernetesAPIProxy)
    proxy.listen_port = 16443
    proxy.listen_host = "127.0.0.1"
    proxy._agent_token = "secret"
    proxy._server_cert_pem = "-----BEGIN CERTIFICATE-----\nlocal\n-----END CERTIFICATE-----\n"
    proxy._temp_files = []
    proxy.server = None
    proxy.server_thread = None
    proxy._agent_kubeconfig_path = None
    path = proxy.generate_agent_kubeconfig()

    try:
        assert Path(path).is_file()
        proxy.stop()
        assert not Path(path).exists()
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/namespaces/demo/pods", True),
        ("/api/v1/namespaces/demo/%70ods", True),
        ("/api/v1/namespaces/demo/%2570ods", True),
        ("/apis/batch/v1/namespaces/demo/jobs", True),
        ("/apis/apps/v1/namespaces/demo/deployments", True),
        ("/apis/apps/v1/namespaces/demo/deployments/demo", False),
        ("/api/v1/namespaces/demo/services", False),
    ],
)
def test_filtered_proxy_blocks_workload_creation_paths(path, expected):
    assert _is_workload_create_path(path) is expected


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


def test_agent_launcher_cleanup_resets_container_runner():
    launcher = AgentLauncher()
    runner = ContainerRunner()
    launcher._container_runner = runner

    launcher.cleanup_all()

    assert launcher._container_runner is None
