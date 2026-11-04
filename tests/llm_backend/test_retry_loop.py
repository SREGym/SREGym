"""Regression tests for #1138: litellm errors must reach the litellm handlers in the retry loop."""

import litellm
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import llm_backend.get_llm_backend as backend_module
from llm_backend.get_llm_backend import LiteLLMBackend, _extract_retry_delay_seconds_from_exception

MODEL = "gemini/gemini-test"
OK = AIMessage(content="ok")
RETRY_INFO = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"}]}}


def rate_limit(body=None):
    error = litellm.RateLimitError(message="rate limited", llm_provider="gemini", model=MODEL)
    error.body = body  # litellm discards the passed response, so feed the provider payload via body
    return error


def trimmed(call):
    return any(isinstance(m, HumanMessage) and m.content == "..." for m in call)


@pytest.fixture
def run(monkeypatch):
    """Runs inference with a scripted ChatLiteLLM; returns (sleeps, messages seen per call)."""
    sleeps, calls, script = [], [], []

    class ScriptedChatLiteLLM:
        def __init__(self, **_kwargs):
            pass

        def invoke(self, input):
            calls.append(list(input))
            outcome = script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(backend_module, "ChatLiteLLM", ScriptedChatLiteLLM)
    monkeypatch.setattr(backend_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(backend_module, "LLM_QUERY_MAX_RETRIES", 5)
    monkeypatch.setattr(backend_module, "LLM_QUERY_INIT_RETRY_DELAY", 1)

    def _run(*outcomes):
        script[:] = [*outcomes, OK]
        messages = [SystemMessage(content="s"), *(HumanMessage(content=f"m{i}") for i in range(40))]
        assert LiteLLMBackend(model_name=MODEL, api_key="k", usage_available=False).inference(messages) is OK
        return sleeps, calls

    return _run


def test_rate_limit_honors_provider_delay_and_trims(run):
    sleeps, calls = run(rate_limit(RETRY_INFO))
    assert sleeps == [45.0]
    assert trimmed(calls[1])


def test_rate_limit_without_provider_delay_backs_off_and_trims(run):
    sleeps, calls = run(rate_limit(), rate_limit(), rate_limit())
    assert sleeps == [1, 2, 4]
    assert all(trimmed(call) for call in calls[1:])


def test_service_unavailable_waits_sixty_seconds_and_trims(run):
    sleeps, calls = run(litellm.ServiceUnavailableError(message="503", llm_provider="gemini", model=MODEL))
    assert sleeps == [60]
    assert trimmed(calls[1])


def test_extract_retry_delay_returns_none_without_retry_info():
    assert _extract_retry_delay_seconds_from_exception(rate_limit()) is None
