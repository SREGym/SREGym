import asyncio
import copy
import json
import sys
import tomllib
from types import SimpleNamespace

import httpx
import pytest

from clients.codex.codex_agent import CodexAgent
from clients.jev.config import INSTRUCTION, KEY_ENV, MODEL_ENV, codex_args, configure_experiment
from clients.jev.server import MAX_CALLS, QUESTIONS, JevEvaluator, create_server, validate_response

QUESTION = {"supported": {"type": "noul", "instructions": "Does the evidence support the claim?"}}
RESPONSE = {
    "model": "jev-test",
    "answers": {"supported": {"type": "noul", "noul": 0.9}},
    "usage": {"input_tokens": 12, "output_tokens": 3},
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (KEY_ENV, MODEL_ENV, "AGENT_API_BASE", "AGENT_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def evaluator(tmp_path, handler):
    return JevEvaluator("jev-test", "private-test-key", tmp_path / "jev.jsonl", transport=httpx.MockTransport(handler))


def test_success_and_audit(tmp_path):
    def handler(request):
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["authorization"] == "Bearer private-test-key"
        assert json.loads(request.content)["state"] == {"observation": "timeout"}
        return httpx.Response(200, json=RESPONSE)

    tool = evaluator(tmp_path, handler)
    result = asyncio.run(tool.evaluate({"observation": "timeout"}, QUESTION))
    assert result["answers"] == RESPONSE["answers"]
    record = json.loads(tool.log_path.read_text())
    assert record["request"]["questions"] == QUESTION
    assert record["result"]["usage"] == RESPONSE["usage"]
    assert record["http_attempts"] == 1
    assert "private-test-key" not in tool.log_path.read_text()


@pytest.mark.parametrize(
    "state,questions",
    [
        ("x", {}),
        ("x", {"q": {"type": "invalid", "instructions": "test"}}),
        ("x", {"q": {"type": "choice", "instructions": "test", "criteria": {"one": "only"}}}),
        ("x" * 66000, QUESTION),
        ("private-test-key", QUESTION),
        ("x", {str(i): QUESTION["supported"] for i in range(17)}),
        (float("nan"), QUESTION),
    ],
)
def test_rejects_invalid_input_without_request(tmp_path, state, questions):
    def handler(_):
        pytest.fail("Invalid input reached provider")

    tool = evaluator(tmp_path, handler)
    assert asyncio.run(tool.evaluate(state, questions))["error"] == "invalid_request"
    assert tool.calls == 0


@pytest.mark.parametrize("status", [401, 403, 422, 302, 429, 529])
def test_provider_errors_and_bounded_retry(tmp_path, monkeypatch, status):
    calls = []

    async def no_sleep(_):
        pass

    monkeypatch.setattr("clients.jev.server.asyncio.sleep", no_sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private-test-key sensitive provider body", headers={"location": "https://elsewhere.test"}
        )

    tool = evaluator(tmp_path, handler)
    result = asyncio.run(tool.evaluate("evidence", QUESTION))
    assert result["http_status"] == status
    assert len(calls) == (2 if status in {429, 529} else 1)
    assert "sensitive provider body" not in tool.log_path.read_text()


def test_retry_then_success(tmp_path, monkeypatch):
    responses = [httpx.Response(429, headers={"Retry-After": "bad"}), httpx.Response(200, json=RESPONSE)]

    async def no_sleep(_):
        pass

    monkeypatch.setattr("clients.jev.server.asyncio.sleep", no_sleep)
    tool = evaluator(tmp_path, lambda _: responses.pop(0))
    assert "answers" in asyncio.run(tool.evaluate("evidence", QUESTION))
    assert json.loads(tool.log_path.read_text())["http_attempts"] == 2


@pytest.mark.parametrize(
    "error,kind", [(httpx.ReadTimeout("secret"), "timeout"), (httpx.ConnectError("secret"), "connection_error")]
)
def test_transport_failure_is_visible_and_not_retried(tmp_path, error, kind):
    def handler(_):
        raise error

    tool = evaluator(tmp_path, handler)
    assert asyncio.run(tool.evaluate("evidence", QUESTION))["error"] == kind
    record = json.loads(tool.log_path.read_text())
    assert record["http_attempts"] == 1
    assert "secret" not in tool.log_path.read_text()


def test_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr("clients.jev.server.CALL_TIMEOUT", 0.01)

    async def handler(_):
        await asyncio.sleep(1)
        return httpx.Response(200, json=RESPONSE)

    tool = evaluator(tmp_path, handler)
    assert asyncio.run(tool.evaluate("evidence", QUESTION))["error"] == "timeout"


@pytest.mark.parametrize("body", [b"not-json", b"x" * 140000, b"{}", json.dumps({**RESPONSE, "answers": {}}).encode()])
def test_invalid_responses(tmp_path, body):
    tool = evaluator(tmp_path, lambda _: httpx.Response(200, content=body))
    assert asyncio.run(tool.evaluate("evidence", QUESTION))["error"] == "invalid_response"


def test_call_budget(tmp_path):
    tool = evaluator(tmp_path, lambda _: pytest.fail("Budget must prevent request"))
    tool.calls = MAX_CALLS
    assert asyncio.run(tool.evaluate("evidence", QUESTION))["error"] == "call_budget_exhausted"


def test_oversized_number_returns_invalid_response(tmp_path):
    response = copy.deepcopy(RESPONSE)
    response["answers"]["supported"]["noul"] = 10**400
    tool = evaluator(tmp_path, lambda _: httpx.Response(200, json=response))
    assert asyncio.run(tool.evaluate("evidence", QUESTION))["error"] == "invalid_response"


def test_all_question_types_and_malformed_probabilities():
    questions = QUESTIONS.validate_python(
        {
            **QUESTION,
            "action": {"type": "choice", "instructions": "Choose", "criteria": {"inspect": None, "wait": "Wait"}},
            "risk": {"type": "score", "instructions": "Assess risk", "criteria": ["Low", "High"]},
        }
    )
    data = copy.deepcopy(RESPONSE)
    data["answers"].update(
        {
            "action": {
                "type": "choice",
                "choice": "inspect",
                "confidence": 0.7,
                "probabilities": {"inspect": 0.8, "wait": 0.2},
            },
            "risk": {"type": "score", "score": 0.1, "confidence": 0.8},
        }
    )
    assert validate_response(data, questions)["answers"]["risk"]["legend"] == {"0": "Low", "1": "High"}
    data["answers"]["action"]["probabilities"]["wait"] = float("nan")
    with pytest.raises(ValueError):
        validate_response(data, questions)


def test_mcp_schema_and_dispatch(tmp_path):
    tool = evaluator(tmp_path, lambda _: httpx.Response(200, json=RESPONSE))
    server = create_server(tool)

    async def check():
        tools = await server.list_tools()
        assert [t.name for t in tools] == ["jev_evaluate"]
        assert tools[0].annotations.readOnlyHint
        result = await server.call_tool("jev_evaluate", {"state": "evidence", "questions": QUESTION})
        assert "0.9" in str(result)

    asyncio.run(check())


def test_opt_in_and_command_config(monkeypatch, tmp_path):
    agent = CodexAgent(tmp_path, "gpt-5.6-luna")
    baseline = agent._build_command("task")
    assert codex_args(tmp_path) == []
    monkeypatch.setenv(MODEL_ENV, "jev-test")
    monkeypatch.setenv(KEY_ENV, "private-test-key")
    monkeypatch.setenv("AGENT_INTERNET_ACCESS", "filtered")
    args = codex_args(tmp_path)
    parsed = tomllib.loads("\n".join(args[1::2]))["mcp_servers"]["jev"]
    assert parsed["required"]
    assert parsed["command"] == sys.executable
    assert "HTTPS_PROXY" in parsed["env_vars"]
    assert "private-test-key" not in str(agent._build_command("task"))
    assert 'web_search="disabled"' in agent._build_command("task")
    assert "plugins" in agent._build_command("task")
    monkeypatch.delenv(MODEL_ENV)
    monkeypatch.delenv("AGENT_INTERNET_ACCESS")
    assert agent._build_command("task") == baseline


def test_prompt_only_changes_when_enabled(monkeypatch):
    from clients.codex.driver import build_instruction

    baseline = build_instruction({"app_name": "test", "namespace": "test"})
    monkeypatch.setenv(MODEL_ENV, "jev-test")
    assert build_instruction({"app_name": "test", "namespace": "test"}) == baseline + INSTRUCTION


def test_configuration_validation_and_reset(monkeypatch):
    args = SimpleNamespace(jev_model="jev-test", agent="codex", use_external_harness=False, force_build=True)
    with pytest.raises(ValueError, match=KEY_ENV):
        configure_experiment(args)
    monkeypatch.setenv(KEY_ENV, "test-key")
    configure_experiment(args)
    assert __import__("os").environ[MODEL_ENV] == "jev-test"
    args.agent = "claudecode"
    with pytest.raises(ValueError, match="Codex"):
        configure_experiment(args)
    args.jev_model = None
    configure_experiment(args)
    assert MODEL_ENV not in __import__("os").environ


@pytest.mark.parametrize("overrides", [{"force_build": False}, {"use_external_harness": True}, {"jev_model": " "}])
def test_unsupported_run_rejected_before_deployment(monkeypatch, overrides):
    monkeypatch.setenv(KEY_ENV, "test-key")
    values = dict(jev_model="jev-test", agent="codex", use_external_harness=False, force_build=True)
    values.update(overrides)
    with pytest.raises(ValueError):
        configure_experiment(SimpleNamespace(**values))


def test_preflight_rejects_api_failure(monkeypatch, tmp_path):
    from clients.jev.server import run_preflight

    monkeypatch.setenv(KEY_ENV, "test-key")
    monkeypatch.setenv(MODEL_ENV, "jev-test")

    async def failure(*_):
        return {"error": "provider_error", "http_status": 401}

    monkeypatch.setattr(JevEvaluator, "evaluate", failure)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        run_preflight(tmp_path / "preflight.jsonl")


def test_mcp_errors_do_not_poison_next_call(tmp_path):
    tool = evaluator(tmp_path, lambda _: httpx.Response(200, json=RESPONSE))
    server = create_server(tool)

    async def check():
        with pytest.raises(Exception, match="validation"):
            await server.call_tool("jev_evaluate", {"state": "evidence", "questions": {}})
        result = await server.call_tool("jev_evaluate", {"state": "evidence", "questions": QUESTION})
        assert "0.9" in str(result)

    asyncio.run(check())


def test_container_forwards_only_when_enabled(monkeypatch):
    from sregym.service.container_runner import ContainerConfig, ContainerRunner
    from sregym.service.internet_policy import InternetPolicy

    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode("open")))
    monkeypatch.setenv(KEY_ENV, "test-key")
    assert not any(KEY_ENV in v for v in runner._build_env_flags())
    monkeypatch.setenv(MODEL_ENV, "jev-test")
    assert f"{KEY_ENV}=test-key" in runner._build_env_flags()
    runner.config.forward_host_credentials = False
    assert not any(KEY_ENV in v for v in runner._build_env_flags())
