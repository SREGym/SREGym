"""Model calls for the mini-sre agent, through LiteLLM.

Replies are streamed, so a long reply keeps the connection busy. SREGym's egress proxy closes a
connection that sends nothing for ten minutes. Each call has a time limit, and rate limit errors
are retried after a wait.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from importlib.metadata import version as package_version
from pathlib import Path

from clients.harness.token_usage import token_count, usage_metrics

DEFAULT_API_MAX_TOKENS = 65536
API_RETRY_WAIT_S = 5
API_RATE_LIMIT_RETRIES = 6
API_RATE_LIMIT_WAIT_S = 60
# Gateway and DNS errors on the way out of the container are retried after a wait.
API_TRANSIENT_RETRIES = 5
API_TRANSIENT_WAIT_S = 30


@dataclass
class Reply:
    """Result of one model call."""

    content: str = ""
    reasoning: str | None = None
    usage: dict = field(default_factory=usage_metrics)
    finish_reason: str | None = None
    latency_s: float = 0.0
    error: str | None = None
    content_from_reasoning: bool = False

    def as_message(self, *, pass_reasoning: bool) -> dict:
        """The assistant message to append to the conversation."""
        message: dict = {"role": "assistant", "content": self.content or ""}
        if pass_reasoning and self.reasoning and not self.content_from_reasoning:
            message["reasoning_content"] = self.reasoning
        return message


def is_rate_limit(exc: Exception) -> bool:
    """Check whether the error is a rate limit error."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "429" in text or "too many requests" in text


_TRANSIENT = (
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "no address associated with hostname",
    "temporary failure in name resolution",
    "connection reset",
    "connection refused",
    "connection error",
    "apiconnectionerror",
    "internalservererror",
    " 502",
    " 503",
    " 504",
)


def is_transient(exc: Exception) -> bool:
    """Check whether the error came from the network or a gateway."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(needle in text for needle in _TRANSIENT)


def parse_api_usage(usage: object) -> dict:
    """Map an OpenAI-style usage block (LiteLLM or raw) to SREGym's token metrics."""

    def get(obj, key):
        """Read a field from a dict or an object."""
        if obj is None:
            return None
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    prompt_details = get(usage, "prompt_tokens_details")
    completion_details = get(usage, "completion_tokens_details")
    return usage_metrics(
        input_tokens=token_count(get(usage, "prompt_tokens")),
        output_tokens=token_count(get(usage, "completion_tokens")),
        cached_input_tokens=token_count(get(prompt_details, "cached_tokens")),
        reasoning_output_tokens=token_count(get(completion_details, "reasoning_tokens")),
    )


def _reasoning_of(message: object) -> str | None:
    """Return the reasoning text of a message or stream delta, if any."""
    reasoning = getattr(message, "reasoning_content", None)
    if not reasoning:
        fields = getattr(message, "provider_specific_fields", None)
        reasoning = fields.get("reasoning_content") if isinstance(fields, dict) else None
    return str(reasoning) if reasoning else None


def _reply_from_response(response) -> Reply:
    """Build a Reply from a LiteLLM response."""
    choice = response.choices[0]
    message = choice.message
    return Reply(
        content=message.content or "",
        reasoning=_reasoning_of(message),
        usage=parse_api_usage(getattr(response, "usage", None)),
        finish_reason=getattr(choice, "finish_reason", None),
    )


class ApiBackend:
    """One LiteLLM chat completion per step."""

    name = "api"

    def __init__(self, model: str, reasoning_effort: str | None = None, *, env: dict[str, str] | None = None):
        """Read the model settings from the environment."""
        source = os.environ if env is None else env
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.api_base = source.get("AGENT_API_BASE") or None
        self.api_key = source.get("AGENT_API_KEY") or None
        self.max_tokens = int(source.get("MINI_SRE_MAX_TOKENS", DEFAULT_API_MAX_TOKENS))
        self.extra_body = json.loads(source.get("MINI_SRE_EXTRA_BODY") or "{}")
        self.timeout_s = int(source.get("MINI_SRE_API_TIMEOUT", "600"))
        # Time limit for a whole call. A provider can keep the connection open without sending
        # anything, and then the HTTP read timeout never fires.
        self.hard_deadline_s = float(source.get("MINI_SRE_API_HARD_DEADLINE_S", str(self.timeout_s + 60)))
        self.stream = source.get("MINI_SRE_API_STREAM", "1") != "0"
        # No temperature is sent unless MINI_SRE_TEMPERATURE is set, so the provider's default is used.
        raw_temperature = source.get("MINI_SRE_TEMPERATURE")
        self.temperature = float(raw_temperature) if raw_temperature not in (None, "") else None

    def version(self) -> str:
        """Return the LiteLLM version."""
        return f"litellm {package_version('litellm')}"

    @staticmethod
    def system_message(text: str) -> dict:
        """Return a system message with this text."""
        return {"role": "system", "content": text}

    def build_request(self, messages: list[dict]) -> dict:
        """Build the arguments for litellm.completion."""
        request: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout_s,
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if self.api_base:
            request["api_base"] = self.api_base
        if self.api_key:
            request["api_key"] = self.api_key
        extra = dict(self.extra_body)
        if self.reasoning_effort and self._translates_reasoning_effort():
            request["reasoning_effort"] = self.reasoning_effort
        elif self.reasoning_effort:
            extra["reasoning_effort"] = self.reasoning_effort
        if extra:
            request["extra_body"] = extra
        return request

    def _translates_reasoning_effort(self) -> bool:
        """Check whether LiteLLM turns ``reasoning_effort`` into the provider's own setting.

        For Anthropic it becomes ``thinking`` and ``output_config.effort``; put in ``extra_body`` it would reach the
        API as an unknown field. For an OpenAI-compatible endpoint LiteLLM does not know (``openai/<model>`` with
        AGENT_API_BASE), it refuses the parameter, so there it goes into the body as it is, as such endpoints read it.
        """
        import litellm

        try:
            model, provider, _, _ = litellm.get_llm_provider(self.model, api_base=self.api_base)
            supported = litellm.get_supported_openai_params(model=model, custom_llm_provider=provider) or []
        except Exception:  # a model LiteLLM cannot place: the body keeps it as it is
            return False
        return "reasoning_effort" in supported

    def complete(self, messages: list[dict], *, step_dir: Path) -> Reply:
        """Send the conversation and save what was sent and what came back."""
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "messages.json").write_text(
            json.dumps(messages, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        request = self.build_request(messages)
        started = time.monotonic()
        reply: Reply | None = None
        error: str | None = None
        plain_attempts = 0
        rate_limit_attempts = 0
        transient_attempts = 0
        while True:
            try:
                reply = self._completion_with_deadline(request)
                error = None
                break
            except Exception as exc:  # provider or transport error; the driver decides what a failed step means
                error = f"model call failed: {type(exc).__name__}: {str(exc)[:300]}"
                if is_rate_limit(exc) and rate_limit_attempts < API_RATE_LIMIT_RETRIES:
                    rate_limit_attempts += 1
                    time.sleep(API_RATE_LIMIT_WAIT_S)
                    continue
                if is_transient(exc) and transient_attempts < API_TRANSIENT_RETRIES:
                    transient_attempts += 1
                    time.sleep(API_TRANSIENT_WAIT_S)
                    continue
                plain_attempts += 1
                if plain_attempts >= 2:
                    break
                time.sleep(API_RETRY_WAIT_S)
        latency = round(time.monotonic() - started, 3)
        if reply is None:
            (step_dir / "stderr.log").write_text(error or "", encoding="utf-8")
            return Reply(latency_s=latency, error=error)
        reply.latency_s = latency
        (step_dir / "answer.json").write_text(
            json.dumps(
                {
                    "content": reply.content,
                    "finish_reason": reply.finish_reason,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        if reply.reasoning:
            (step_dir / "reasoning.txt").write_text(reply.reasoning, encoding="utf-8")
        if not reply.content and reply.reasoning and reply.finish_reason != "length":
            # Some thinking models put the whole reply in reasoning_content and leave content
            # empty. Use the reasoning as the reply so the step is not lost.
            reply.content = reply.reasoning
            reply.content_from_reasoning = True
        if not reply.content:
            reply.error = "empty model reply" + (
                f" (output truncated at max_tokens={self.max_tokens})" if reply.finish_reason == "length" else ""
            )
        return reply

    def _completion_with_deadline(self, request: dict) -> Reply:
        """Make one call and stop waiting after the time limit."""
        if self.stream:
            return self._stream(request)
        import threading

        import litellm

        box: dict = {}

        def work() -> None:
            """Make the call and keep its result or error."""
            try:
                box["response"] = litellm.completion(**request)
            except Exception as exc:  # reported by the caller
                box["exception"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(self.hard_deadline_s)
        if worker.is_alive():  # the thread dies with the process
            raise TimeoutError(f"no reply within the hard deadline of {self.hard_deadline_s:.0f}s")
        if "exception" in box:
            raise box["exception"]
        return _reply_from_response(box["response"])

    def _stream(self, request: dict) -> Reply:
        """Read the streamed chunks, checking the time limit between them, and build the reply."""
        import litellm

        deadline = time.monotonic() + self.hard_deadline_s
        chunks = []
        stream = litellm.completion(**request, stream=True, stream_options={"include_usage": True})
        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"reply still streaming at the hard deadline of {self.hard_deadline_s:.0f}s")
                chunks.append(chunk)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        if not chunks:
            raise RuntimeError("empty stream")
        response = litellm.stream_chunk_builder(chunks, messages=request["messages"])
        reply = _reply_from_response(response)
        if reply.reasoning is None:  # the builder may drop provider fields; take them from the deltas
            pieces = []
            for chunk in chunks:
                for choice in getattr(chunk, "choices", None) or []:
                    text = _reasoning_of(getattr(choice, "delta", None))
                    if text:
                        pieces.append(text)
            reply.reasoning = "".join(pieces) or None
        if reply.usage.get("input_tokens") is None:
            usage = next((getattr(c, "usage", None) for c in reversed(chunks) if getattr(c, "usage", None)), None)
            reply.usage = parse_api_usage(usage)
        return reply
