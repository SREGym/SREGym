import pytest

from sregym.service.internet_policy import EndpointRule, InternetPolicy
from sregym.service.provider_endpoints import PROVIDERS, provider_endpoint_rules


def test_codex_subscription_allows_only_runtime_and_refresh_hosts():
    policy = InternetPolicy.from_mode("filtered", agent_name="codex", model_id="gpt-5.6-sol")

    rules = provider_endpoint_rules(
        policy,
        {"OPENAI_API_KEY": "unused-when-subscription-auth-exists"},
        codex_auth_available=True,
    )

    assert rules == (
        EndpointRule("auth.openai.com", 443),
        EndpointRule("chatgpt.com", 443),
    )


def test_codex_api_key_allows_openai_api_only():
    policy = InternetPolicy.from_mode("filtered", agent_name="codex", model_id="gpt-5.6-sol")

    assert provider_endpoint_rules(policy, {"OPENAI_API_KEY": "secret"}) == (EndpointRule("api.openai.com", 443),)


def test_codex_custom_endpoint_replaces_openai_hosts():
    policy = InternetPolicy.from_mode("filtered", agent_name="codex", model_id="custom-model")

    assert provider_endpoint_rules(
        policy,
        {"AGENT_API_BASE": "https://models.example.test/v1", "OPENAI_API_KEY": "unused"},
        codex_auth_available=True,
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


@pytest.mark.parametrize("provider,definition", [(name, item) for name, item in PROVIDERS.items() if item.default_url])
def test_provider_definition_keeps_default_and_overrides_together(provider, definition):
    policy = InternetPolicy.from_mode("filtered", agent_name="stratus", model_id=f"{provider}/model")
    assert provider_endpoint_rules(policy, {}) == (EndpointRule.host_from_url(definition.default_url),)
    for variable in definition.environment_variables:
        assert provider_endpoint_rules(policy, {variable: "https://custom.test/v1"}) == (
            EndpointRule("custom.test", 443),
        )
