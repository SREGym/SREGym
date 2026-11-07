"""Regression tests for #1138: litellm errors must reach the litellm handlers in the retry loop."""

import copy
import json

import httpx
import litellm
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from requests.exceptions import HTTPError

import llm_backend.get_llm_backend as backend_module
from llm_backend.get_llm_backend import LiteLLMBackend

MODEL = "gemini/gemini-test"
OK = AIMessage(content="ok")
RETRY_INFO = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"}]}}


def rate_limit(payload=None):
    """Build the error the way litellm does: provider body embedded in a prefixed message."""
    message = "rate limited" if payload is None else f"VertexAIException - {json.dumps(payload)}"
    return litellm.RateLimitError(message=message, llm_provider="gemini", model=MODEL)


@pytest.fixture(params=[0, 14, 15, 20], ids=lambda rounds: f"{rounds}-tool-rounds")
def messages(request):
    """Match Stratus's initial task followed by assistant/tool exchanges."""
    history = [
        SystemMessage(content="Investigate and repair the application."),
        HumanMessage(content="Repair Astronomy Shop in astronomy-shop. Diagnosis: the ad CPU limit is too low."),
    ]
    for index in range(request.param):
        history.extend(
            [
                AIMessage(
                    content="Inspecting the workload.",
                    tool_calls=[{"name": "inspect", "args": {}, "id": f"call_{index}"}],
                ),
                ToolMessage(content="Workload inspected.", tool_call_id=f"call_{index}"),
            ]
        )
    return history


@pytest.fixture
def run(monkeypatch, messages):
    """Runs inference with a scripted ChatLiteLLM; returns (sleeps, messages seen per call)."""
    sleeps, calls, script = [], [], []

    class ScriptedChatLiteLLM:
        def __init__(self, **_kwargs):
            pass

        def invoke(self, input):
            calls.append(copy.deepcopy(input))
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
        original = copy.deepcopy(messages)
        assert LiteLLMBackend(model_name=MODEL, api_key="k", usage_available=False).inference(messages) is OK
        assert len(calls) == len(outcomes) + 1
        assert all(call == original for call in calls)
        assert messages == original
        return sleeps, calls

    return _run


def test_rate_limit_honors_provider_delay_and_preserves_messages(run):
    sleeps, _ = run(rate_limit(RETRY_INFO))
    assert sleeps == [45.0]


def test_rate_limit_without_provider_delay_backs_off_and_preserves_messages(run):
    sleeps, _ = run(rate_limit(), rate_limit())
    assert sleeps == [1, 2]


def test_service_unavailable_waits_sixty_seconds_and_preserves_messages(run):
    sleeps, _ = run(litellm.ServiceUnavailableError(message="503", llm_provider="gemini", model=MODEL))
    assert sleeps == [60]


def test_mixed_transient_errors_preserve_messages(run):
    sleeps, _ = run(
        rate_limit(RETRY_INFO),
        litellm.ServiceUnavailableError(message="503", llm_provider="gemini", model=MODEL),
        rate_limit(),
        rate_limit(),
    )
    assert sleeps == [45.0, 60, 1, 2]


@pytest.mark.parametrize("delay", ["0s", "-1s", "invalid"])
def test_unusable_provider_delay_uses_backoff(run, delay):
    payload = {"error": {"details": [{"@type": "google.rpc.RetryInfo", "retryDelay": delay}]}}
    sleeps, _ = run(rate_limit(payload), rate_limit(payload))
    assert sleeps == [1, 2]


@pytest.mark.parametrize("error_type", [openai.RateLimitError, openai.APIError, HTTPError])
def test_general_handlers_still_back_off_without_changing_messages(run, error_type):
    request = httpx.Request("POST", "https://example.invalid/chat/completions")
    if error_type is openai.RateLimitError:
        error = error_type("rate limited", response=httpx.Response(429, request=request), body=None)
    elif error_type is openai.APIError:
        error = error_type("provider failed", request=request, body=None)
    else:
        error = error_type("HTTP error")
    sleeps, _ = run(error, error)
    assert sleeps == [1, 2]
