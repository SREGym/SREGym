import asyncio
import base64
import json
import stat
from pathlib import Path

import pytest
import yaml

from clients.claudecode.claudecode_agent import ClaudeCodeAgent
from clients.codex.codex_agent import CodexAgent
from clients.copilot.copilot_agent import CopilotCliAgent
from clients.geminicli.geminicli_agent import GeminiCliAgent
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import AgentRegistration
from sregym.conductor.conductor import ConductorConfig
from sregym.service.container_runner import (
    ContainerConfig,
    ContainerRunner,
)
from sregym.service.docker_egress import PROXY_BUNDLE_CONTAINER_PATH, PROXY_CA_CONTAINER_PATH, _find_host_ca_bundle
from sregym.service.internet_policy import (
    EndpointRule,
    InternetPolicy,
    endpoint_host_is_allowed,
    endpoint_is_allowed,
    provider_tool_uses_internet,
)
from sregym.service.k8s_proxy import KubernetesAPIProxy, _is_workload_create_path, is_valid_bearer_token


def test_programmatic_filtered_conductor_enables_kubernetes_restrictions():
    assert ConductorConfig().restrict_network_access
    assert not ConductorConfig(internet_policy=InternetPolicy.from_mode("open")).restrict_network_access
    assert ConductorConfig(
        internet_policy=InternetPolicy.from_mode("open"), block_workload_creation=True
    ).restrict_network_access


def test_endpoint_rules_require_matching_host_port_and_path():
    rules = (EndpointRule.from_url("https://api.example.test/v1"),)

    assert endpoint_host_is_allowed("API.EXAMPLE.TEST.", 443, rules)
    assert endpoint_is_allowed("api.example.test", 443, "/v1/messages", rules)
    assert not endpoint_is_allowed("api.example.test", 443, "/v10/messages", rules)
    assert not endpoint_is_allowed("api.example.test", 443, "/v1/%252e%252e/private", rules)
    assert not endpoint_is_allowed("api.example.test", 80, "/v1/messages", rules)
    assert not endpoint_is_allowed("other.example.test", 443, "/v1/messages", rules)


def test_exact_endpoint_rule_does_not_allow_other_paths():
    rules = (EndpointRule("api.example.test", 443, "/", include_subpaths=False),)

    assert endpoint_is_allowed("api.example.test", 443, "/", rules)
    assert not endpoint_is_allowed("api.example.test", 443, "/models", rules)


def test_provider_host_rule_ignores_configured_base_path():
    rule = EndpointRule.host_from_url("https://provider.example.test/v1")

    assert rule == EndpointRule("provider.example.test", 443)
    assert rule.allows("provider.example.test", 443, "/oauth/token")


def test_internet_policy_rejects_invalid_additional_endpoint():
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        InternetPolicy.from_mode(
            "filtered",
            additional_allowed_endpoints=["ssh://example.test"],
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"tools":[{"type":"web_search_preview"}]}',
        b'{"tools":[{"type":"web_search_20250305"}]}',
        b'{"tools":[{"type":"web_fetch_20250910"}]}',
        b'{"tools":[{"googleSearch":{}}]}',
        b'{"tools":[{"type":"mcp","server_url":"https://example.com/mcp"}]}',
        b'{"tools":[{"type":"computer_use_preview"}]}',
        b'{"plugins":[{"id":"github"}]}',
        b'{"apps":[{"id":"github"}]}',
        b'{"mcp_servers":[{"url":"https://example.com/mcp"}]}',
    ],
)
def test_detects_provider_hosted_web_tools(body):
    assert provider_tool_uses_internet(body)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_detects_web_tools_in_json_encodings(encoding):
    assert provider_tool_uses_internet('{"tools":[{"type":"web_search"}]}'.encode(encoding))


def test_overly_nested_requests_fail_closed():
    assert provider_tool_uses_internet('{"response":' * 2000 + "{}" + "}" * 2000)


def test_provider_tool_filter_does_not_scan_prompt_text_or_regular_tools():
    body = (
        b'{"messages":[{"content":"use web_search"}],"tools":[{"type":"function","name":"kubectl",'
        b'"parameters":{"properties":{"browser":{"type":"string"}}}}]}'
    )
    assert not provider_tool_uses_internet(body)


def test_large_provider_requests_do_not_skip_tool_inspection():
    body = b" " * 1_000_001 + b'{"tools":[{"type":"web_search"}]}'
    assert provider_tool_uses_internet(body)
    assert not provider_tool_uses_internet(b" " * 1_000_001 + b'{"tools":[{"type":"function","name":"kubectl"}]}')


@pytest.mark.parametrize("wrapper", ["response", "session", "request"])
def test_provider_event_wrappers_are_inspected(wrapper):
    assert provider_tool_uses_internet(json.dumps({wrapper: {"tools": [{"type": "web_search"}]}}))
    assert not provider_tool_uses_internet(json.dumps({"messages": [{wrapper: {"tools": [{"type": "web_search"}]}}]}))


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
    runner._egress._network_name = "private-network"
    runner._egress._proxy_name = "filter-proxy"
    runner._egress._proxy_ca = tmp_path / "proxy-ca.pem"
    runner._egress._ca_bundle = tmp_path / "ca-bundle.pem"
    runner._egress._proxy_ca.touch()
    runner._egress._ca_bundle.touch()

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
    runner._egress._network_name = "private-network"
    runner._egress._proxy_name = "filter-proxy"
    runner._egress._proxy_ca = tmp_path / "proxy-ca.pem"
    runner._egress._ca_bundle = tmp_path / "ca-bundle.pem"
    runner._egress._proxy_ca.touch()
    runner._egress._ca_bundle.touch()

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
        runner._egress._prepare_state()
        assert runner._egress._tmp_dir is not None
        blocked_log = runner._egress._tmp_dir / "blocked-requests.jsonl"
        policy_file = runner._egress._tmp_dir / "policy.json"

        assert stat.S_IMODE(runner._egress._tmp_dir.stat().st_mode) == 0o711
        assert stat.S_IMODE(blocked_log.stat().st_mode) == 0o622
        assert not (runner._egress._tmp_dir / "observed-requests.jsonl").exists()
        assert json.loads(policy_file.read_text()) == {"allowed_endpoints": []}
    finally:
        runner.cleanup_egress_proxy()


def test_filtered_runner_builds_rules_from_explicit_provider_endpoints():
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode(
                "filtered",
                agent_name="stratus",
                model_id="openai/local-model",
            )
        )
    )
    rules = runner._configured_egress_rules(
        {"AGENT_API_BASE": "https://custom.example.test/v1", "API_PORT": "8000", "MCP_SERVER_PORT": "9954"}
    )

    assert EndpointRule("custom.example.test", 443) in rules
    assert EndpointRule("host.docker.internal", 8000) in rules
    assert EndpointRule("host.docker.internal", 9954) in rules
    assert EndpointRule("host.docker.internal", 16443) in rules


def test_filtered_runner_adds_user_endpoints_without_replacing_required_rules():
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode(
                "filtered",
                agent_name="stratus",
                model_id="openai/gpt-5",
                additional_allowed_endpoints=[
                    "https://telemetry.example.test/v1",
                    "http://localhost:18081/health",
                ],
            )
        )
    )

    rules = runner._configured_egress_rules({"API_PORT": "8000", "MCP_SERVER_PORT": "9954"})

    assert EndpointRule("api.openai.com", 443) in rules
    assert EndpointRule("telemetry.example.test", 443, "/v1") in rules
    assert EndpointRule("host.docker.internal", 18081, "/health") in rules
    assert EndpointRule("host.docker.internal", 8000) in rules


def test_additional_endpoint_path_does_not_allow_sibling_paths():
    policy = InternetPolicy.from_mode(
        "filtered",
        additional_allowed_endpoints=["https://telemetry.example.test/v1"],
    )
    rule = EndpointRule.from_url(policy.additional_allowed_endpoints[0])

    assert rule.allows("telemetry.example.test", 443, "/v1/events")
    assert not rule.allows("telemetry.example.test", 443, "/admin")


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


def test_agent_container_does_not_receive_judge_credentials(monkeypatch):
    monkeypatch.setenv("JUDGE_API_BASE", "https://judge.example.test/v1")
    monkeypatch.setenv("JUDGE_API_KEY", "judge-secret")
    monkeypatch.setenv("JUDGE_MODEL_ID", "judge-model")
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("open")))

    env = dict(item.split("=", 1) for item in runner._build_env_flags()[1::2])

    assert "JUDGE_API_BASE" not in env
    assert "JUDGE_API_KEY" not in env
    assert "JUDGE_MODEL_ID" not in env


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
    runner._egress._network_name = "private-network"
    runner._egress._proxy_name = "filter-proxy"
    runner._egress._proxy_ca = tmp_path / "proxy-ca.pem"
    runner._egress._ca_bundle = tmp_path / "ca-bundle.pem"
    runner._egress._proxy_ca.touch()
    runner._egress._ca_bundle.touch()

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
    assert "WebFetch" not in ClaudeCodeAgent.allowed_tools()
    assert "WebSearch" not in ClaudeCodeAgent.allowed_tools()


def test_open_mode_keeps_claude_web_search(monkeypatch):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "open")
    assert "WebFetch" in ClaudeCodeAgent.allowed_tools()
    assert "WebSearch" in ClaudeCodeAgent.allowed_tools()


def test_filtered_mode_disables_codex_hosted_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "filtered")
    agent = CodexAgent(logs_dir=tmp_path, model_name="gpt-5.6-sol")

    command = agent._build_command("inspect the cluster")
    option_pairs = list(zip(command, command[1:], strict=False))

    assert 'web_search="disabled"' in command
    assert ("--disable", "apps") in option_pairs
    assert ("--disable", "plugins") in option_pairs


def test_open_mode_keeps_codex_hosted_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "open")
    agent = CodexAgent(logs_dir=tmp_path, model_name="gpt-5.6-sol")

    command = agent._build_command("inspect the cluster")
    option_pairs = list(zip(command, command[1:], strict=False))

    assert 'web_search="disabled"' not in command
    assert ("--disable", "apps") not in option_pairs
    assert ("--disable", "plugins") not in option_pairs


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
