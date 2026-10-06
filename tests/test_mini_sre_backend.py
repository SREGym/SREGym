import json
import time
from types import SimpleNamespace

import litellm
import pytest

from clients.mini_sre import backends
from clients.mini_sre.backends import ApiBackend, Reply

ENV = {
    "AGENT_API_BASE": "https://api.z.ai/api/paas/v4",
    "AGENT_API_KEY": "secret-key-123",
    "MINI_SRE_EXTRA_BODY": '{"thinking": {"type": "enabled"}}',
}


def usage(prompt=1500, completion=400, cached=1000, reasoning=350):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def response(content="", reasoning=None, finish="stop", use=None):
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=use or usage())


def test_build_request():
    backend = ApiBackend("openai/glm-5.3", "high", env={**ENV, "MINI_SRE_MAX_TOKENS": "4096"})
    request = backend.build_request([{"role": "user", "content": "hi"}])
    assert request["model"] == "openai/glm-5.3" and request["api_base"] == ENV["AGENT_API_BASE"]
    assert request["max_tokens"] == 4096 and "tools" not in request
    assert "temperature" not in request  # the provider's default is used
    pinned = ApiBackend("openai/glm-5.3", None, env={**ENV, "MINI_SRE_TEMPERATURE": "0"})
    assert pinned.build_request([{"role": "user", "content": "hi"}])["temperature"] == 0.0
    assert request["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert "secret-key-123" not in json.dumps(request["messages"])


def test_non_streaming_reply(tmp_path, monkeypatch):
    backend = ApiBackend("openai/glm-5.3", None, env={**ENV, "MINI_SRE_API_STREAM": "0"})
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return response("look\n```bash\nls\n```", reasoning="thinking...")

    monkeypatch.setattr(litellm, "completion", fake)
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "s1")
    assert reply.error is None and reply.content == "look\n```bash\nls\n```" and reply.reasoning == "thinking..."
    assert (
        reply.usage["input_tokens"] == 1500
        and reply.usage["cached_input_tokens"] == 1000
        and reply.usage["reasoning_output_tokens"] == 350
    )
    assert "stream" not in seen and "tools" not in seen
    assert (tmp_path / "s1" / "reasoning.txt").read_text() == "thinking..."
    assert json.loads((tmp_path / "s1" / "answer.json").read_text())["content"].startswith("look")


def test_streaming_reply(tmp_path, monkeypatch):
    backend = ApiBackend("openai/deepseek-flash", "high", env=ENV)
    assert backend.stream is True
    seen = {}

    def chunk(content=None, reasoning=None, use=None):
        delta = SimpleNamespace(content=content, reasoning_content=reasoning)
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=None)] if content or reasoning else [], usage=use
        )

    def fake_stream(**kwargs):
        seen.update(kwargs)
        yield chunk(reasoning="think ")
        yield chunk(reasoning="more")
        yield chunk(content="done")
        yield chunk(use=usage(900, 120, 704, 80))

    def fake_builder(chunks, messages=None):
        built = response("done", reasoning=None)
        built.usage = None  # usage comes from the last chunk when the builder drops it
        return built

    monkeypatch.setattr(litellm, "completion", fake_stream)
    monkeypatch.setattr(litellm, "stream_chunk_builder", fake_builder)
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "s1")
    assert seen["stream"] is True and seen["stream_options"] == {"include_usage": True}
    assert reply.error is None and reply.content == "done"
    assert reply.reasoning == "think more"  # taken from the deltas when the builder drops it
    assert reply.usage["input_tokens"] == 900 and reply.usage["cached_input_tokens"] == 704


def test_empty_reply_is_an_error(tmp_path, monkeypatch):
    backend = ApiBackend("openai/glm-5.3", None, env={**ENV, "MINI_SRE_API_STREAM": "0", "MINI_SRE_MAX_TOKENS": "64"})
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response("", finish="length"))
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "s1")
    assert reply.error == "empty model reply (output truncated at max_tokens=64)"


def test_hard_deadline_non_streaming(tmp_path, monkeypatch):
    backend = ApiBackend(
        "openai/glm-5.3", None, env={**ENV, "MINI_SRE_API_STREAM": "0", "MINI_SRE_API_HARD_DEADLINE_S": "0.2"}
    )
    monkeypatch.setattr(backends, "API_RETRY_WAIT_S", 0)

    def hangs(**kwargs):
        time.sleep(2)
        return None

    monkeypatch.setattr(litellm, "completion", hangs)
    started = time.monotonic()
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "s1")
    assert time.monotonic() - started < 1.5 and "hard deadline" in (reply.error or "")
    assert "hard deadline" in (tmp_path / "s1" / "stderr.log").read_text()


def test_hard_deadline_streaming_closes_the_stream(tmp_path, monkeypatch):
    backend = ApiBackend("openai/glm-5.3", None, env={**ENV, "MINI_SRE_API_HARD_DEADLINE_S": "0.1"})
    monkeypatch.setattr(backends, "API_RETRY_WAIT_S", 0)
    closed = []

    class SlowStream:
        def __iter__(self):
            while True:
                time.sleep(0.06)
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(delta=SimpleNamespace(content=".", reasoning_content=None), finish_reason=None)
                    ],
                    usage=None,
                )

        def close(self):
            closed.append(True)

    monkeypatch.setattr(litellm, "completion", lambda **kwargs: SlowStream())
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "s1")
    assert "hard deadline" in (reply.error or "") and closed == [True, True]


def test_rate_limit_retries(tmp_path, monkeypatch):
    backend = ApiBackend("openai/glm-5.3", None, env={**ENV, "MINI_SRE_API_STREAM": "0"})
    monkeypatch.setattr(backends, "API_RATE_LIMIT_WAIT_S", 0)
    monkeypatch.setattr(backends, "API_RETRY_WAIT_S", 0)
    monkeypatch.setattr(backends, "API_RATE_LIMIT_RETRIES", 3)
    calls = []

    class RateLimitError(Exception):
        pass

    def limited(**kwargs):
        calls.append(1)
        raise RateLimitError("429 Too Many Requests")

    monkeypatch.setattr(litellm, "completion", limited)
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "s1")
    assert len(calls) == 5 and "RateLimitError" in (reply.error or "")  # 3 rate limit retries + 2 attempts

    calls.clear()

    def limited_then_ok(**kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise RateLimitError("429")
        return response("ok")

    monkeypatch.setattr(litellm, "completion", limited_then_ok)
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "s2")
    assert reply.error is None and reply.content == "ok" and len(calls) == 3


def test_reply_as_message():
    reply = Reply(content="x", reasoning="r")
    assert reply.as_message(pass_reasoning=True) == {"role": "assistant", "content": "x", "reasoning_content": "r"}
    assert reply.as_message(pass_reasoning=False) == {"role": "assistant", "content": "x"}


@pytest.mark.parametrize(
    "text,expected", [("RateLimitError: 429", True), ("rate limit exceeded", True), ("BadRequestError: 400", False)]
)
def test_is_rate_limit(text, expected):
    assert backends.is_rate_limit(Exception(text)) is expected


def test_reasoning_only_reply_is_used_as_content(tmp_path, monkeypatch):
    backend = ApiBackend("openai/deepseek-flash", "high", env={**ENV, "MINI_SRE_API_STREAM": "0"})
    only_reasoning = response("", reasoning="THOUGHT\n\n```bash\nkubectl get pods\n```")
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: only_reasoning)
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "r1")
    assert reply.error is None and reply.content_from_reasoning
    assert "kubectl get pods" in reply.content
    assert "reasoning_content" not in reply.as_message(pass_reasoning=True)


def test_truncated_empty_reply_is_an_error(tmp_path, monkeypatch):
    backend = ApiBackend("openai/deepseek-flash", "high", env={**ENV, "MINI_SRE_API_STREAM": "0"})
    monkeypatch.setattr(
        litellm, "completion", lambda **kwargs: response("", reasoning="still thinking", finish="length")
    )
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "r2")
    assert reply.error is not None and "max_tokens" in reply.error and not reply.content_from_reasoning


def test_gateway_error_is_retried(tmp_path, monkeypatch):
    backend = ApiBackend("openai/deepseek-flash", "high", env={**ENV, "MINI_SRE_API_STREAM": "0"})
    assert backends.is_transient(
        Exception("BadGatewayError: 502 Bad Gateway [Errno -5] No address associated with hostname")
    )
    assert backends.is_transient(Exception("APIConnectionError: connection reset by peer"))
    assert not backends.is_transient(Exception("BadRequestError: your prompt is malformed"))
    monkeypatch.setattr(backends.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 4:
            raise Exception("BadGatewayError: 502 Bad Gateway")
        return response("recovered")

    monkeypatch.setattr(litellm, "completion", flaky)
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "t1")
    assert reply.error is None and reply.content == "recovered" and calls["n"] == 4


def test_gateway_error_fails_after_retries(tmp_path, monkeypatch):
    backend = ApiBackend("openai/deepseek-flash", "high", env={**ENV, "MINI_SRE_API_STREAM": "0"})
    monkeypatch.setattr(backends.time, "sleep", lambda _: None)

    def always(**kwargs):
        raise Exception("BadGatewayError: 502 Bad Gateway")

    monkeypatch.setattr(litellm, "completion", always)
    reply = backend.complete([{"role": "user", "content": "go"}], step_dir=tmp_path / "t2")
    assert reply.error is not None and "BadGateway" in reply.error
