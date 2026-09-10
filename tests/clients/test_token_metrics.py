"""Client results and standalone ATIF exports must use the same token totals."""

import json
import shutil
from pathlib import Path

import pytest

from atif_converter import convert
from atif_converter.adapters import claudecode, codex, copilot, gemini, opencode, stratus
from clients.claudecode.claudecode_agent import ClaudeCodeAgent
from clients.codex.codex_agent import CodexAgent
from clients.copilot.copilot_agent import CopilotCliAgent
from clients.cursor.cursor_agent import CursorAgent
from clients.geminicli.geminicli_agent import GeminiCliAgent
from clients.harness.token_usage import aggregate_usage, usage_metrics
from clients.opencode.opencode_agent import OpenCodeAgent

FIXTURES = Path(__file__).parents[1] / "traces" / "fixtures"


def assert_totals_match(usage, trajectory):
    final = trajectory.final_metrics
    assert usage["input_tokens"] == final.total_prompt_tokens
    assert usage["output_tokens"] == final.total_completion_tokens
    assert usage["cached_input_tokens"] == final.total_cached_tokens
    expected_total = (
        usage["input_tokens"] + usage["output_tokens"]
        if usage["input_tokens"] is not None and usage["output_tokens"] is not None
        else None
    )
    assert usage["total_tokens"] == expected_total
    assert usage["token_metrics_version"] == 2


@pytest.mark.parametrize("agent_name", ["opencode", "claudecode", "codex", "gemini"])
def test_recorded_run_matches_atif(agent_name, tmp_path):
    shutil.copytree(FIXTURES / f"{agent_name}_run", tmp_path, dirs_exist_ok=True)
    if agent_name == "opencode":
        agent = OpenCodeAgent(tmp_path, "openai/example")
        trajectory = opencode.convert_file(next((tmp_path / "sessions").rglob("session-*.json")))
    elif agent_name == "claudecode":
        agent = ClaudeCodeAgent(tmp_path, "example")
        trajectory = claudecode.convert_files(list((tmp_path / "sessions/projects/-logs").glob("*.jsonl")))
    elif agent_name == "codex":
        agent = CodexAgent(tmp_path, "example")
        trajectory = codex.convert_file(next((tmp_path / "sessions").rglob("*.jsonl")))
    else:
        agent = GeminiCliAgent(tmp_path, "example", gemini_home=tmp_path)
        session = next((tmp_path / "sessions").rglob("session-*"))
        home_sessions = tmp_path / "tmp/chats"
        home_sessions.mkdir(parents=True)
        shutil.copy(session, home_sessions / session.name)
        trajectory = gemini.convert_file(session)
    assert_totals_match(agent.get_usage_metrics(), trajectory)


@pytest.mark.parametrize(
    "agent_class", [OpenCodeAgent, ClaudeCodeAgent, CodexAgent, GeminiCliAgent, CopilotCliAgent, CursorAgent]
)
def test_missing_usage_is_unknown(agent_class, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agent = agent_class(tmp_path, "openai/example")
    usage = agent.get_usage_metrics()
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["cached_input_tokens"] is None
    assert usage["total_tokens"] is None
    assert usage["token_metrics_version"] == 2


@pytest.mark.parametrize(
    "tokens",
    [
        {"input": 100, "output": 40, "reasoning": 10, "cache": {"read": 50, "write": 20}},
        {"input": 0, "output": 0, "reasoning": 10, "cache": {"read": 0, "write": 20}},
        {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    ],
)
def test_opencode_split_tokens(tokens, tmp_path):
    agent = OpenCodeAgent(tmp_path, "openai/example")
    finish = {"type": "step-finish", "tokens": tokens}
    agent.output_path.write_text(json.dumps({"type": "step_finish", "part": finish}) + "\n")
    usage = agent.get_usage_metrics()
    metrics, _ = opencode._metrics_from_finish(finish)
    assert usage["input_tokens"] == tokens["input"] + sum(tokens["cache"].values())
    assert usage["output_tokens"] == tokens["output"] + tokens["reasoning"]
    assert usage["reasoning_output_tokens"] == tokens["reasoning"]
    assert usage["cache_creation_input_tokens"] == tokens["cache"]["write"]
    assert metrics.prompt_tokens == usage["input_tokens"]
    assert metrics.completion_tokens == usage["output_tokens"]
    assert metrics.cached_tokens == usage["cached_input_tokens"]


def test_claude_streaming_usage_counted_once(tmp_path):
    agent = ClaudeCodeAgent(tmp_path, "example")
    project = agent.sessions_dir / "projects/test"
    project.mkdir(parents=True)
    events = [
        {
            "type": "assistant",
            "message": {
                "id": "same",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 20,
                    "output_tokens": output,
                },
            },
        }
        for output in (1, 40, 40)
    ]
    (project / "session.jsonl").write_text("\n".join(map(json.dumps, events)))
    usage = agent.get_usage_metrics()
    assert usage["input_tokens"] == 170
    assert usage["output_tokens"] == 40
    assert usage["cached_input_tokens"] == 50
    assert usage["cache_creation_input_tokens"] == 20
    assert usage["reasoning_output_tokens"] is None


def test_gemini_tool_prompt_tokens_are_input(tmp_path):
    agent = GeminiCliAgent(tmp_path, "example", gemini_home=tmp_path)
    chats = tmp_path / "tmp/chats"
    chats.mkdir(parents=True)
    session = chats / "session-test.json"
    session.write_text(
        json.dumps(
            {
                "sessionId": "test",
                "messages": [
                    {
                        "type": "gemini",
                        "content": "ok",
                        "tokens": {
                            "input": 100,
                            "output": 40,
                            "thoughts": 10,
                            "tool": 20,
                            "cached": 50,
                        },
                    }
                ],
            }
        )
    )
    usage = agent.get_usage_metrics()
    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 50
    assert usage["reasoning_output_tokens"] == 10
    assert_totals_match(usage, gemini.convert_file(session))


@pytest.mark.parametrize("fixture", ["copilot_run_flat", "copilot_run_real"])
def test_recorded_copilot_stream_matches_atif(fixture, tmp_path):
    shutil.copytree(FIXTURES / fixture, tmp_path, dirs_exist_ok=True)
    agent = CopilotCliAgent(tmp_path, "example")
    assert_totals_match(agent.get_usage_metrics(), copilot.convert_file(agent.jsonl_path))


@pytest.mark.parametrize("reasoning_key", ["gen_ai.usage.reasoning.output_tokens", "gen_ai.usage.reasoning_tokens"])
def test_copilot_does_not_add_parent_totals_or_repeated_spans(tmp_path, reasoning_key):
    agent = CopilotCliAgent(tmp_path, "example")
    agent.otel_dir.mkdir()
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 40,
        "gen_ai.usage.cache_read.input_tokens": 50,
        "gen_ai.usage.cache_creation.input_tokens": 20,
        reasoning_key: 10,
    }
    chat = {"span_id": "chat1", "attributes": attrs}
    parent = {"span_id": "parent", "attributes": {**attrs, "gen_ai.operation.name": "invoke_agent"}}
    (agent.otel_dir / "spans.jsonl").write_text("\n".join(map(json.dumps, [chat, parent, chat])))
    usage = agent.get_usage_metrics()
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 40
    assert usage["total_tokens"] == 140
    assert usage["cached_input_tokens"] == 50
    assert usage["cache_creation_input_tokens"] == 20
    assert usage["reasoning_output_tokens"] == 10
    # Native CLI streams can omit input usage entirely. The independent
    # converter must get those counts from the explicitly supplied telemetry.
    agent.jsonl_path.write_text(
        json.dumps({"type": "assistant.message", "data": {"content": "ok", "outputTokens": 40}}) + "\n"
    )
    trajectory = convert(agent.jsonl_path, telemetry_files=[agent.otel_dir / "spans.jsonl"])
    assert_totals_match(usage, trajectory)
    assert trajectory.final_metrics.extra["reasoning_tokens"] == 10
    assert trajectory.final_metrics.extra["cache_write_tokens"] == 20


@pytest.mark.parametrize("reported_input", [100, 0])
def test_copilot_telemetry_replaces_stream_totals_without_losing_unreported_fields(tmp_path, reported_input):
    from sregym.traces.convert import convert_run

    run = tmp_path / "results/b/copilot/problem/run_1"
    agent = CopilotCliAgent(run, "example")
    agent.jsonl_path.write_text(
        json.dumps({"type": "message", "role": "assistant", "content": "ok"})
        + "\n"
        + json.dumps({"type": "usage", "input_tokens": 90, "output_tokens": 40})
        + "\n"
    )
    agent.otel_dir.mkdir()
    span = {
        "type": "span",
        "spanId": "one",
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": reported_input,
        },
    }
    telemetry = agent.otel_dir / "one.jsonl"
    telemetry.write_text(json.dumps(span) + '\n{"truncated":')
    # The same span can occur in overlapping exported files.
    (agent.otel_dir / "two.jsonl").write_text(json.dumps(span) + "\n[]\n")
    usage = agent.get_usage_metrics()
    assert usage["input_tokens"] == reported_input
    assert usage["output_tokens"] == 40
    trajectory = convert_run(run)
    assert_totals_match(usage, trajectory)
    assert trajectory.final_metrics.total_prompt_tokens == reported_input


def test_copilot_irrelevant_telemetry_preserves_stream_usage(tmp_path):
    agent = CopilotCliAgent(tmp_path, "example")
    agent.jsonl_path.write_text(
        json.dumps({"type": "message", "role": "assistant", "content": "ok"})
        + "\n"
        + json.dumps({"type": "usage", "input_tokens": 100, "output_tokens": 40})
        + "\n"
    )
    agent.otel_dir.mkdir()
    telemetry = agent.otel_dir / "spans.jsonl"
    telemetry.write_text(
        json.dumps({"attributes": {"gen_ai.operation.name": "invoke_agent", "gen_ai.usage.input_tokens": 999}})
    )
    trajectory = convert(agent.jsonl_path, telemetry_files=[telemetry])
    assert_totals_match(agent.get_usage_metrics(), trajectory)
    assert trajectory.final_metrics.total_prompt_tokens == 100


def test_copilot_native_usage_replaces_message_estimate(tmp_path):
    agent = CopilotCliAgent(tmp_path, "example")
    events = [
        {"type": "assistant.message", "data": {"content": "ok", "outputTokens": 40}},
        {
            "type": "assistant.usage",
            "data": {
                "apiCallId": "one-call",
                "inputTokens": 100,
                "outputTokens": 40,
                "cacheReadTokens": 50,
                "cacheWriteTokens": 20,
                "reasoningTokens": 10,
            },
        },
    ]
    agent.jsonl_path.write_text("\n".join(map(json.dumps, [*events, events[-1]])))
    usage = agent.get_usage_metrics()
    assert usage["total_tokens"] == 140
    assert_totals_match(usage, copilot.convert_file(agent.jsonl_path))


def test_opencode_without_session_aggregate_uses_steps(tmp_path):
    agent = OpenCodeAgent(tmp_path, "openai/example")
    agent.sessions_dir.mkdir()
    session = agent.sessions_dir / "session-test.json"
    messages = [
        {
            "info": {"role": "assistant"},
            "parts": [
                {
                    "type": "step-finish",
                    "tokens": {
                        "input": 100,
                        "output": 40,
                        "reasoning": 10,
                        "cache": {"read": 50, "write": 20},
                    },
                }
            ],
        }
        for _ in range(2)
    ]
    session.write_text(json.dumps({"info": {"id": "test"}, "messages": messages}))
    usage = agent.get_usage_metrics()
    assert usage["input_tokens"] == 340
    assert usage["output_tokens"] == 100
    assert_totals_match(usage, opencode.convert_file(session))


def test_opencode_stream_does_not_double_count_duplicate_or_legacy_usage(tmp_path):
    agent = OpenCodeAgent(tmp_path, "openai/example")
    finish = {
        "type": "step_finish",
        "part": {
            "id": "one",
            "tokens": {
                "input": 100,
                "output": 40,
                "reasoning": 10,
                "cache": {"read": 50, "write": 20},
            },
        },
    }
    lines = [finish, finish, {"usage": {"input_tokens": 170, "output_tokens": 50}}, {"part": "invalid"}, []]
    agent.output_path.write_text("\n".join(map(json.dumps, lines)) + '\n{"interrupted":')
    usage = agent.get_usage_metrics()
    assert usage["total_tokens"] == 220


@pytest.mark.parametrize("value", [0, 100])
def test_codex_uses_latest_cumulative_usage_once(tmp_path, value):
    agent = CodexAgent(tmp_path, "example")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = sessions / "rollout-test.jsonl"
    usage = {"input_tokens": value, "output_tokens": value, "cached_input_tokens": 0, "reasoning_output_tokens": 0}
    events = [
        {"type": "session_meta", "payload": {"id": "test"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
        },
        *[{"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage}}}] * 3,
    ]
    session.write_text("\n".join(map(json.dumps, events)) + '\n{"unfinished":')
    metrics = agent.get_usage_metrics()
    assert metrics["total_tokens"] == value * 2
    assert_totals_match(metrics, codex.convert_file(session))


def test_codex_does_not_select_an_unrelated_session(tmp_path):
    agent = CodexAgent(tmp_path, "example")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions/other.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 999, "output_tokens": 999}},
                },
            }
        )
    )
    agent.output_path.write_text(
        "\n".join(
            map(
                json.dumps,
                [
                    {"type": "thread.started", "thread_id": "current"},
                    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 40}},
                ],
            )
        )
    )
    assert agent.get_usage_metrics()["total_tokens"] == 140


def test_inclusive_totals_do_not_add_breakdowns_again():
    metrics = usage_metrics(
        input_tokens=100,
        output_tokens=40,
        cached_input_tokens=50,
        cache_creation_input_tokens=20,
        reasoning_output_tokens=10,
    )
    assert metrics["total_tokens"] == 140
    assert aggregate_usage([metrics, metrics])["total_tokens"] == 280
    assert aggregate_usage([])["total_tokens"] is None


def test_stratus_keeps_langchain_totals_and_breakdowns():
    metrics = stratus._metrics_from_usage(
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "input_token_details": {"cache_read": 50, "cache_creation": 20},
            "output_token_details": {"reasoning": 10},
        },
        None,
    )
    assert metrics.prompt_tokens == 100
    assert metrics.completion_tokens == 40
    assert metrics.cached_tokens == 50
    assert metrics.extra["reasoning_tokens"] == 10
    assert metrics.extra["cache_write_tokens"] == 20
