import asyncio
import copy
import json
import sys
import tomllib
from types import SimpleNamespace

import httpx
import pytest

from clients.codex.codex_agent import CodexAgent
from clients.jev.client import MAX_CALLS, QUESTIONS, JevEvaluator, validate_response
from clients.jev.config import INSTRUCTION, KEY_ENV, MODEL_ENV, codex_args, configure
from clients.jev.planning import DiagnosticTest, planning_guidance, planning_questions
from clients.jev.review import collect_snapshot, compact_snapshot, review_guidance, review_questions
from clients.jev.server import create_server
from clients.jev.submission import SubmissionClient

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
        ("x", {"q": {"type": "score", "instructions": "test", "criteria": ["level"] * 11}}),
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

    monkeypatch.setattr("clients.jev.client.asyncio.sleep", no_sleep)

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

    monkeypatch.setattr("clients.jev.client.asyncio.sleep", no_sleep)
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
    monkeypatch.setattr("clients.jev.client.CALL_TIMEOUT", 0.01)

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


def test_mcp_exposes_only_supported_tools(tmp_path):
    tool = evaluator(tmp_path, lambda _: pytest.fail("Unexpected provider call"))
    server = create_server(tool)

    async def check():
        tools = await server.list_tools()
        assert {t.name for t in tools} == {"jev_plan", "jev_submit"}
        assert {t.name: t.annotations.readOnlyHint for t in tools} == {"jev_plan": True, "jev_submit": False}

    asyncio.run(check())


def test_review_snapshot_excludes_credentials_and_annotations():
    rows = compact_snapshot(
        {
            "items": [
                {
                    "kind": "Deployment",
                    "metadata": {"name": "app", "annotations": {"secret": "private-annotation"}},
                    "spec": {
                        "replicas": 1,
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "app",
                                        "image": "image",
                                        "env": [{"name": "KEY", "value": "private-env"}],
                                        "command": ["private-command"],
                                        "resources": {"limits": {"memory": "64Mi"}},
                                    }
                                ]
                            }
                        },
                    },
                },
                {"kind": "Secret", "metadata": {"name": "private-secret"}, "data": {"key": "private-data"}},
                {
                    "kind": "Service",
                    "metadata": {"name": "app"},
                    "spec": {"selector": {"app": "app"}, "internalTrafficPolicy": "Cluster"},
                },
            ]
        }
    )
    text = json.dumps(rows)
    assert "private-" not in text
    assert rows[0]["containers"][0]["resources"]["limits"]["memory"] == "64Mi"
    assert rows[1]["spec"]["internalTrafficPolicy"] == "Cluster"


@pytest.mark.parametrize("namespace", ["--all-namespaces", "app;id", "", "x" * 64])
def test_review_rejects_invalid_namespace_before_command(monkeypatch, namespace):
    monkeypatch.setattr("clients.jev.review.subprocess.run", lambda *a, **kw: pytest.fail("Unexpected command"))
    with pytest.raises(ValueError):
        asyncio.run(collect_snapshot(namespace))


def test_review_snapshot_uses_existing_kubectl_connection(monkeypatch):
    def command(args, **kwargs):
        assert args[:2] == ["kubectl", "get"]
        assert args[3:] == ["--namespace", "app", "--request-timeout=10s", "-o", "json"]
        assert "secrets" not in args[2]
        assert kwargs["timeout"] == 12
        assert "env" not in kwargs and not kwargs.get("shell")
        return SimpleNamespace(returncode=0, stdout='{"items": []}')

    monkeypatch.setattr("clients.jev.review.subprocess.run", command)
    snapshot = asyncio.run(collect_snapshot("app"))
    assert snapshot["namespace"] == "app" and snapshot["resources"] == []


def test_review_questions_cover_durability():
    questions = review_questions("verify")
    QUESTIONS.validate_python(questions)
    assert set(questions) == {"causal_support", "durable_repair", "functional_evidence"}


def test_review_empty_endpoint_slice():
    rows = compact_snapshot({"items": [{"kind": "EndpointSlice", "metadata": {"name": "empty"}, "endpoints": None}]})
    assert rows[0]["endpoints"] == []


@pytest.mark.parametrize(
    "score, expected", [(0.4, "unsupported"), (0.5, "unsupported"), (0.6, "uncertain"), (0.7, "supported")]
)
def test_review_guidance_does_not_confuse_ranking_with_causality(score, expected):
    result = {
        "answers": {
            "causal_support": {"noul": score},
            "active_failure": {"noul": 0.9},
            "next_component": {"confidence": 0.99},
        }
    }
    assert review_guidance(result, "diagnose")["assessment"] == expected


def test_review_guidance_requires_functional_evidence_for_recovery():
    result = {
        "answers": {
            key: {"noul": value}
            for key, value in (
                ("causal_support", 0.9),
                ("active_failure", 0.9),
                ("durable_repair", 0.9),
                ("functional_evidence", 0.1),
            )
        }
    }
    assert review_guidance(result, "verify")["assessment"] == "unsupported"
    assert review_guidance({"error": "timeout"}, "verify")["assessment"] == "unavailable"


@pytest.mark.parametrize("phase", ["diagnose", "verify"])
def test_decision_review_does_not_return_triage_rankings(phase):
    questions = review_questions(phase)
    QUESTIONS.validate_python(questions)
    assert all(question["type"] == "noul" for question in questions.values())
    assert ("active_failure" in questions) == (phase != "verify")


def test_review_rejects_causal_claim_without_active_failure():
    result = {"answers": {"causal_support": {"noul": 0.9}, "active_failure": {"noul": 0.1}}}
    assert review_guidance(result, "diagnose")["assessment"] == "unsupported"


def test_review_snapshot_failure_does_not_call_provider(tmp_path, monkeypatch):
    async def fail(_):
        raise ValueError("Do not expose raw credential-bearing errors")

    monkeypatch.setattr("clients.jev.server.collect_snapshot", fail)
    tool = evaluator(tmp_path, lambda _: pytest.fail("Unexpected provider call"))
    result = asyncio.run(
        create_server(tool).call_tool(
            "jev_submit",
            {
                "namespace": "app",
                "stage": "diagnosis",
                "diagnosis": "Requests fail",
                "observations": [],
            },
        )
    )
    assert "snapshot_unavailable" in str(result)
    assert "credential-bearing" not in str(result)
    assert tool.calls == 0


@pytest.mark.parametrize("stage", ["diagnosis", "mitigation"])
@pytest.mark.parametrize(
    "score,expected", [(0.2, False), (0.5, False), (0.51, False), (0.69, False), (0.7, True), (0.8, True)]
)
def test_submission_gate(tmp_path, monkeypatch, stage, score, expected):
    async def snapshot(_):
        return {"resources": []}

    monkeypatch.setattr("clients.jev.server.collect_snapshot", snapshot)

    def handler(request):
        questions = json.loads(request.content)["questions"]
        return httpx.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "answers": {name: {"type": "noul", "noul": score} for name in questions},
            },
        )

    submitted = []

    class Submitter:
        async def submit(self, requested, solution):
            submitted.append((requested, solution))
            return {"status": "accepted", "stage": requested}

    tool = evaluator(tmp_path, handler)
    asyncio.run(
        create_server(tool, submitter=Submitter()).call_tool(
            "jev_submit",
            {
                "namespace": "app",
                "stage": stage,
                "observations": "fresh requests",
                "diagnosis": "cause",
                "applied_action": "repair",
            },
        )
    )
    assert submitted == ([(stage, "cause" if stage == "diagnosis" else "")] if expected else [])
    record = json.loads(tool.log_path.read_text().splitlines()[-1])
    assert record["result"]["status"] == ("accepted" if expected else "not_submitted")


@pytest.mark.parametrize("stage", ["diagnosis", "mitigation"])
def test_rejected_submission_requires_successful_new_plan(tmp_path, monkeypatch, stage):
    async def snapshot(_):
        return {"resources": []}

    monkeypatch.setattr("clients.jev.server.collect_snapshot", snapshot)
    calls = []
    posted = []
    reject_plan = True

    async def evaluate(state, questions):
        calls.append(questions)
        if "next_test" in questions:
            selected = "revise_tests" if reject_plan else "test_1"
            return {
                "answers": {
                    "next_test": {
                        "choice": selected,
                        "confidence": 1.0,
                        "probabilities": {key: float(key == selected) for key in questions["next_test"]["criteria"]},
                    }
                }
            }
        score = 0.2 if len(calls) == 1 else 0.9
        return {"answers": {key: {"noul": score} for key in questions}}

    class Submitter:
        async def submit(self, requested, solution):
            posted.append(requested)
            return {"status": "accepted", "stage": requested}

    tool = evaluator(tmp_path, lambda _: pytest.fail("Unexpected provider request"))
    monkeypatch.setattr(tool, "evaluate", evaluate)
    server = create_server(tool, submitter=Submitter())
    arguments = {
        "namespace": "app",
        "stage": stage,
        "observations": "evidence",
        "diagnosis": "cause",
        "applied_action": "repair",
    }
    tests = [
        {"hypothesis": f"cause {i}", "command": f"read {i}", "supports_if": "failure", "rejects_if": "success"}
        for i in range(3)
    ]

    async def check():
        nonlocal reject_plan
        await server.call_tool("jev_submit", arguments)
        blocked = await server.call_tool("jev_submit", {**arguments, "diagnosis": "reworded cause"})
        assert "next_tool" in str(blocked) and "jev_plan" in str(blocked)
        assert len(calls) == 1 and not posted
        await server.call_tool("jev_plan", {"namespace": "app", "observations": "new evidence", "tests": tests})
        await server.call_tool("jev_submit", arguments)
        assert len(calls) == 2 and not posted  # Unhelpful planning does not reset the workflow.
        reject_plan = False
        await server.call_tool("jev_plan", {"namespace": "app", "observations": "new evidence", "tests": tests})
        await server.call_tool("jev_submit", arguments)
        assert len(calls) == 4 and posted == [stage]

    asyncio.run(check())


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"status": "200", "stage": "diagnosis"}, "accepted"),
        (200, {"status": "200", "stage": "mitigation"}, "submission_unknown"),
        (200, {"status": "no"}, "submission_unknown"),
        (409, {}, "not_submitted"),
        (503, {}, "not_submitted"),
    ],
)
def test_submission_requires_conductor_acceptance(status, body, expected):
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"stage": "diagnosis"})
        posted.append(json.loads(request.content))
        return httpx.Response(status, json=body)

    client = SubmissionClient(transport=httpx.MockTransport(handler))

    async def check():
        assert (await client.submit("diagnosis", "cause"))["status"] == expected
        if expected == "accepted":
            assert (await client.submit("diagnosis", "cause"))["status"] == expected

    asyncio.run(check())
    assert posted == [{"stage": "diagnosis", "solution": "cause"}]


def test_submission_does_not_queue_future_stage():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"stage": "diagnosis"})

    client = SubmissionClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(client.submit("mitigation", ""))["status"] == "not_submitted"


def test_ambiguous_submission_does_not_retry():
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"stage": "diagnosis"})
        posted.append(request)
        raise httpx.ReadTimeout("lost")

    client = SubmissionClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(client.submit("diagnosis", "cause"))["status"] == "submission_unknown"
    assert len(posted) == 1


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
    assert parsed["enabled_tools"] == ["jev_plan", "jev_submit"]
    assert "private-test-key" not in str(agent._build_command("task"))
    assert 'web_search="disabled"' in agent._build_command("task")
    assert "plugins" in agent._build_command("task")
    monkeypatch.delenv(MODEL_ENV)
    monkeypatch.delenv("AGENT_INTERNET_ACCESS")
    assert agent._build_command("task") == baseline


@pytest.mark.parametrize("confidence,count", [(0.49, 2), (0.5, 1), (0.9, 1)])
def test_planning_interprets_rankings_without_executing(confidence, count):
    tests = [DiagnosticTest(hypothesis=str(i), command=f"read {i}", supports_if="x", rejects_if="y") for i in range(3)]
    questions = planning_questions(tests)
    QUESTIONS.validate_python(questions)
    assert questions["next_test"]["type"] == "choice"
    assert all(questions[f"value_{i}"]["type"] == "score" for i in range(1, 4))
    result = {
        "answers": {
            "next_test": {
                "choice": "test_2",
                "confidence": confidence,
                "probabilities": {"test_1": 0.2, "test_2": 0.5, "test_3": 0.1, "revise_tests": 0.2},
            }
        }
    }
    guidance = planning_guidance(result, tests)
    assert len(guidance["tests"]) == count
    assert guidance["tests"][0]["command"] == "read 1"
    result["answers"]["next_test"]["choice"] = "revise_tests"
    assert planning_guidance(result, tests)["tests"] == []
    assert "unavailable" in planning_guidance({"error": "timeout"}, tests)["next_step"]


def test_planning_dispatch(tmp_path, monkeypatch):
    async def snapshot(_):
        return {"resources": []}

    monkeypatch.setattr("clients.jev.server.collect_snapshot", snapshot)

    def handler(request):
        body = json.loads(request.content)
        assert len(body["state"]["candidate_tests"]) == 3
        answers = {
            "next_test": {
                "type": "choice",
                "choice": "test_1",
                "confidence": 0.9,
                "probabilities": {"test_1": 0.9, "test_2": 0.05, "test_3": 0.04, "revise_tests": 0.01},
            }
        }
        answers.update({f"value_{i}": {"type": "score", "score": 2.0, "confidence": 0.6} for i in range(1, 4)})
        return httpx.Response(
            200, json={"model": "jev-test", "answers": answers, "usage": {"input_tokens": 3, "output_tokens": 4}}
        )

    tool = evaluator(tmp_path, handler)
    result = asyncio.run(
        create_server(tool).call_tool(
            "jev_plan",
            {
                "namespace": "app",
                "observations": "fresh evidence",
                "tests": [
                    {"hypothesis": str(i), "command": f"read {i}", "supports_if": "x", "rejects_if": "y"}
                    for i in range(3)
                ],
            },
        )
    )
    assert "read 0" in str(result)
    assert json.loads(tool.log_path.read_text().splitlines()[-1])["tool"] == "jev_plan"


def test_prompt_only_changes_when_enabled(monkeypatch):
    from clients.codex.driver import build_instruction

    baseline = build_instruction({"app_name": "test", "namespace": "test"})
    monkeypatch.setenv(MODEL_ENV, "jev-test")
    enabled = build_instruction({"app_name": "test", "namespace": "test"})
    assert enabled.endswith(INSTRUCTION)
    assert "Use `jev_plan`" in enabled
    assert "Use `jev_submit` for both stages" in enabled
    assert "Example: POST" not in enabled
    assert "Example: POST" in baseline
    monkeypatch.delenv(MODEL_ENV)
    assert build_instruction({"app_name": "test", "namespace": "test"}) == baseline


def test_configuration_validation_and_reset(monkeypatch):
    args = SimpleNamespace(jev_model="jev-test", agent="codex", use_external_harness=False, force_build=True)
    with pytest.raises(ValueError, match=KEY_ENV):
        configure(args)
    monkeypatch.setenv(KEY_ENV, "test-key")
    configure(args)
    assert __import__("os").environ[MODEL_ENV] == "jev-test"
    args.agent = "claudecode"
    with pytest.raises(ValueError, match="Codex"):
        configure(args)
    args.jev_model = None
    configure(args)
    assert MODEL_ENV not in __import__("os").environ


@pytest.mark.parametrize("overrides", [{"force_build": False}, {"use_external_harness": True}, {"jev_model": " "}])
def test_unsupported_run_rejected_before_deployment(monkeypatch, overrides):
    monkeypatch.setenv(KEY_ENV, "test-key")
    values = dict(jev_model="jev-test", agent="codex", use_external_harness=False, force_build=True)
    values.update(overrides)
    with pytest.raises(ValueError):
        configure(SimpleNamespace(**values))


def test_preflight_rejects_api_failure(monkeypatch, tmp_path):
    from clients.jev.server import run_preflight

    monkeypatch.setenv(KEY_ENV, "test-key")
    monkeypatch.setenv(MODEL_ENV, "jev-test")

    async def failure(*_):
        return {"error": "provider_error", "http_status": 401}

    monkeypatch.setattr(JevEvaluator, "evaluate", failure)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        run_preflight(tmp_path / "preflight.jsonl")


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
