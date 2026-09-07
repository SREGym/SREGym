import json
from pathlib import Path

import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.internet_policy import EndpointRule, InternetPolicy
from sregym.service.provider_endpoints import PROVIDERS, provider_endpoint_rules


def test_codex_subscription_allows_only_runtime_and_refresh_hosts():
    policy = InternetPolicy.from_mode("filtered", agent_name="codex", model_id="gpt-5.6-sol")

    rules = provider_endpoint_rules(
        policy,
        {"OPENAI_API_KEY": "unused-when-subscription-auth-exists"},
        codex_subscription_auth=True,
    )

    assert rules == (
        EndpointRule("auth.openai.com", 443),
        EndpointRule("chatgpt.com", 443),
    )


def test_codex_api_key_allows_openai_api_only():
    policy = InternetPolicy.from_mode("filtered", agent_name="codex", model_id="gpt-5.6-sol")

    assert provider_endpoint_rules(policy, {"OPENAI_API_KEY": "secret"}) == (EndpointRule("api.openai.com", 443),)


@pytest.mark.parametrize(
    "auth,subscription",
    [
        ({"OPENAI_API_KEY": "test-key"}, False),
        ({"auth_mode": "apikey", "OPENAI_API_KEY": "test-key"}, False),
        ({"OPENAI_API_KEY": "test-key", "tokens": {"access_token": "test-token"}}, False),
        ({"OPENAI_API_KEY": None, "tokens": {"access_token": "test-token"}}, True),
        ({"auth_mode": "chatgpt", "tokens": {"access_token": "test-token"}}, True),
        ({"auth_mode": "chatgptAuthTokens", "tokens": {"access_token": "test-token"}}, True),
        ({"auth_mode": "chatgpt", "OPENAI_API_KEY": "unused", "tokens": {}}, True),
        ({}, False),
        (None, False),
        ([], False),
    ],
)
def test_codex_runner_uses_stored_auth_type(monkeypatch, tmp_path, auth, subscription):
    auth_file = tmp_path / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    original = json.dumps(auth)
    auth_file.write_text(original)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("filtered", agent_name="codex")))

    # A stored login takes precedence over the API key in the environment.
    rules = runner._configured_egress_rules({"OPENAI_API_KEY": "environment-test-key"})

    assert (EndpointRule("api.openai.com", 443) in rules) is not subscription
    assert (EndpointRule("chatgpt.com", 443) in rules) is subscription
    assert (EndpointRule("auth.openai.com", 443) in rules) is subscription
    assert auth_file.read_text() == original


@pytest.mark.parametrize("state", ["missing", "invalid-json", "symlink", "unreadable"])
def test_codex_runner_does_not_infer_subscription_from_unusable_file(monkeypatch, tmp_path, state):
    auth_file = tmp_path / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if state == "invalid-json":
        auth_file.write_text("not json")
    elif state == "symlink":
        target = tmp_path / "other-auth.json"
        target.write_text('{"tokens": {}}')
        auth_file.symlink_to(target)
    elif state == "unreadable":
        auth_file.write_text('{"tokens": {}}')
        monkeypatch.setattr("sregym.service.container_runner.os.access", lambda *_: False)
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("filtered", agent_name="codex")))

    rules = runner._configured_egress_rules({"OPENAI_API_KEY": "test-key"})

    assert EndpointRule("api.openai.com", 443) in rules
    assert EndpointRule("chatgpt.com", 443) not in rules


def test_codex_custom_endpoint_replaces_openai_hosts():
    policy = InternetPolicy.from_mode("filtered", agent_name="codex", model_id="custom-model")

    assert provider_endpoint_rules(
        policy,
        {"AGENT_API_BASE": "https://models.example.test/v1", "OPENAI_API_KEY": "unused"},
        codex_subscription_auth=True,
    ) == (EndpointRule("models.example.test", 443),)


def test_claude_oauth_includes_provider_refresh_host():
    policy = InternetPolicy.from_mode("filtered", agent_name="claudecode", model_id="claude-opus-4-8")

    assert provider_endpoint_rules(policy, {"CLAUDE_CODE_OAUTH_TOKEN": "oauth"}) == (
        EndpointRule("api.anthropic.com", 443),
        EndpointRule("platform.claude.com", 443),
    )


@pytest.mark.parametrize(
    ("model", "environment", "expected_hosts"),
    [
        ("gpt-5", {}, {"api.openai.com"}),
        ("anthropic/claude-sonnet-4-6", {}, {"api.anthropic.com"}),
        ("gemini/gemini-2.5-pro", {}, {"generativelanguage.googleapis.com"}),
        ("deepseek/deepseek-reasoner", {}, {"api.deepseek.com"}),
        (
            "bedrock/us.anthropic.claude-sonnet-4-5-v1:0",
            {"AWS_DEFAULT_REGION": "us-east-2"},
            {"bedrock-runtime.us-east-2.amazonaws.com", "sts.us-east-2.amazonaws.com"},
        ),
        (
            "vertex_ai/gemini-2.5-pro",
            {"VERTEXAI_LOCATION": "us-west1"},
            {"us-west1-aiplatform.googleapis.com", "oauth2.googleapis.com"},
        ),
    ],
)
def test_stratus_provider_allowlist_is_derived_from_selected_model(model, environment, expected_hosts):
    policy = InternetPolicy.from_mode("filtered", agent_name="stratus", model_id=model)

    rules = provider_endpoint_rules(policy, environment)

    assert {rule.host for rule in rules} == expected_hosts


def test_unrelated_provider_endpoint_does_not_enter_allowlist():
    policy = InternetPolicy.from_mode("filtered", agent_name="stratus", model_id="anthropic/claude-sonnet-4-6")

    rules = provider_endpoint_rules(
        policy,
        {
            "ANTHROPIC_API_KEY": "secret",
            "OPENAI_API_BASE": "https://unrelated.example.test/v1",
        },
    )

    assert rules == (EndpointRule("api.anthropic.com", 443),)


def test_unknown_provider_fails_closed_with_configuration_help():
    policy = InternetPolicy.from_mode("filtered", agent_name="stratus", model_id="unknown/model")

    with pytest.raises(ValueError, match="AGENT_API_BASE"):
        provider_endpoint_rules(policy, {})


@pytest.mark.parametrize(
    ("model", "environment", "expected_hosts"),
    [
        ("google/gemini-2.5-pro", {}, {"generativelanguage.googleapis.com"}),
        ("llama/Llama-4-Maverick", {}, {"api.llama.com"}),
        ("opencode/gpt-5", {}, {"opencode.ai"}),
        ("local/qwen3-coder", {"AGENT_API_BASE": "http://127.0.0.1:11434/v1"}, {"127.0.0.1"}),
    ],
)
def test_opencode_provider_allowlist_is_derived_from_selected_model(model, environment, expected_hosts):
    policy = InternetPolicy.from_mode("filtered", agent_name="opencode", model_id=model)

    rules = provider_endpoint_rules(policy, environment)

    assert {rule.host for rule in rules} == expected_hosts


def test_open_mode_does_not_build_provider_allowlist():
    policy = InternetPolicy.from_mode("open", agent_name="stratus", model_id="unknown/model")

    assert provider_endpoint_rules(policy, {}) == ()


@pytest.mark.parametrize("location", ["global", "us", "eu", "us-central1", "europe-west4"])
def test_vertex_allowlist_matches_the_client_destination(location):
    from litellm.llms.vertex_ai.common_utils import get_vertex_base_url

    policy = InternetPolicy.from_mode("filtered", agent_name="stratus", model_id="vertex_ai/gemini-2.5-pro")
    rules = provider_endpoint_rules(policy, {"VERTEXAI_LOCATION": location})
    assert EndpointRule.host_from_url(get_vertex_base_url(location)) in rules


@pytest.mark.parametrize("provider,definition", [(name, item) for name, item in PROVIDERS.items() if item.default_url])
def test_provider_definition_keeps_default_and_overrides_together(provider, definition):
    policy = InternetPolicy.from_mode("filtered", agent_name="stratus", model_id=f"{provider}/model")
    assert provider_endpoint_rules(policy, {}) == (EndpointRule.host_from_url(definition.default_url),)
    for variable in definition.environment_variables:
        assert provider_endpoint_rules(policy, {variable: "https://custom.test/v1"}) == (
            EndpointRule("custom.test", 443),
        )
