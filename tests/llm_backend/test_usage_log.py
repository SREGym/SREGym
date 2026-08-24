import json

import pytest
from langchain_core.messages import AIMessage

import llm_backend.get_llm_backend as backend_module
from llm_backend.get_llm_backend import LiteLLMBackend
from llm_backend.usage_log import USAGE_LOG_PATH_ENV, summarize_usage


class FakeChatLiteLLM:
    def __init__(self, **_kwargs):
        pass

    def invoke(self, input):
        return AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
                "input_token_details": {"cache_read": 2},
            },
            response_metadata={"model_name": "provider-model"},
        )


class FailingChatLiteLLM(FakeChatLiteLLM):
    def invoke(self, input):
        raise RuntimeError("request failed")


def test_inference_persists_each_completed_response(monkeypatch, tmp_path):
    usage_log = tmp_path / "stratus_usage.jsonl"
    monkeypatch.setenv(USAGE_LOG_PATH_ENV, str(usage_log))
    monkeypatch.setattr(backend_module, "ChatLiteLLM", FakeChatLiteLLM)

    backend = LiteLLMBackend("configured-model")
    backend.inference("first", usage_type="summary")
    backend.inference("second", usage_type="tool")

    records = [json.loads(line) for line in usage_log.read_text().splitlines()]
    assert [record["usage_type"] for record in records] == ["summary", "tool"]
    assert records[0]["model"] == "provider-model"
    assert records[0]["input_tokens"] == 7
    assert records[0]["input_token_details"] == {"cache_read": 2}
    assert summarize_usage(usage_log) == {
        "requests": 2,
        "input_tokens": 14,
        "output_tokens": 6,
        "total_tokens": 20,
        "duration_seconds": pytest.approx(records[0]["duration_seconds"] + records[1]["duration_seconds"]),
    }
    assert summarize_usage(usage_log, usage_type="summary")["total_tokens"] == 10


def test_failed_request_does_not_create_usage_record(monkeypatch, tmp_path):
    usage_log = tmp_path / "stratus_usage.jsonl"
    monkeypatch.setenv(USAGE_LOG_PATH_ENV, str(usage_log))
    monkeypatch.setattr(backend_module, "ChatLiteLLM", FailingChatLiteLLM)

    with pytest.raises(RuntimeError, match="request failed"):
        LiteLLMBackend("configured-model").inference("fail")

    assert not usage_log.exists()


def test_summarize_usage_ignores_partial_final_line(tmp_path):
    usage_log = tmp_path / "stratus_usage.jsonl"
    usage_log.write_text(
        json.dumps(
            {
                "usage_type": "trace",
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
                "duration_seconds": 0.5,
            }
        )
        + "\n"
        + '{"usage_type": "trace"'
    )

    assert summarize_usage(usage_log) == {
        "requests": 1,
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
        "duration_seconds": 0.5,
    }
