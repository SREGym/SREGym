"""Generative LLM for escalation and the report write-up.

Default backend (JEV_AGENT_LLM_BACKEND=codex): the Codex CLI on the ChatGPT subscription, run through
llm_backend.judge_bridge._run_codex (the same invocation SREGym's subscription judge uses; API keys are stripped
from its environment). The agent image installs the CLI via install-codex.sh and the container gets a copy of
~/.codex/auth.json. Token usage is not reported by this path.

Alternative backend (JEV_AGENT_LLM_BACKEND=api): OpenAI Responses API over `requests`, described below.

Used at most JEV_AGENT_MAX_LLM_CALLS times per run (escalation and the report write-up). The key comes from
DECISIONS_OPENAI_API_KEY (the variable the decisions backend already forwards) or OPENAI_API_KEY.

Request shape (verified against gpt-6-luna):
    POST https://api.openai.com/v1/responses
    {"model": ..., "input": [{"role": "system", "content": ...}, {"role": "user", "content": ...}],
     "max_output_tokens": N, "reasoning": {"effort": "low"},
     "text": {"format": {"type": "json_object"}}}   # format only for JSON calls
gpt-6-luna is a reasoning model (default effort medium): reasoning tokens count against max_output_tokens, so
budgets are generous and an answer cut off with no text ("status": "incomplete") is an error.
The reply text is the concatenation of output[*].content[*].text items of type "output_text";
usage is {"input_tokens", "output_tokens", ...}.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("all.jev_agent.llm")

DEFAULT_URL = "https://api.openai.com/v1/responses"
URL_ENV = "JEV_AGENT_LLM_URL"
MODEL_ENV = "JEV_AGENT_LLM_MODEL"
EFFORT_ENV = "JEV_AGENT_LLM_EFFORT"  # reasoning effort; "none" omits the parameter
DEFAULT_EFFORT = "low"
KEY_ENVS = ("DECISIONS_OPENAI_API_KEY", "OPENAI_API_KEY")
BACKEND_ENV = "JEV_AGENT_LLM_BACKEND"  # "codex" (subscription, default) | "api"
DEFAULT_BACKEND = "codex"


def backend() -> str:
    """The configured LLM backend name (JEV_AGENT_LLM_BACKEND)."""
    return os.environ.get(BACKEND_ENV, DEFAULT_BACKEND).strip().lower() or DEFAULT_BACKEND


@dataclass
class LLMResponse:
    """One LLM answer: its text, token usage, model, latency, and the parsed JSON object when asked for JSON."""

    text: str
    usage: dict = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0
    data: dict | None = None  # parsed JSON object for complete_json


def api_key() -> str:
    """The first non-empty API key among the supported environment variables, or an empty string."""
    for name in KEY_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def parse_json_object(text: str) -> dict | None:
    """The whole text as a JSON object, else the first balanced {...} object in it."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            value, _ = decoder.raw_decode(text, idx)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        idx = text.find("{", idx + 1)
    return None


def output_text(body: dict) -> str:
    """The text of a Responses API body (its output_text, or the joined output_text parts)."""
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    parts = []
    for item in body.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


class LLMClient:
    """LLM client over the OpenAI Responses API, with retries and token accounting."""

    def __init__(
        self,
        model: str | None = None,
        *,
        key: str | None = None,
        url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        """Configure the client for `model`; ValueError when no model or API key is set."""
        self.model = (model or os.environ.get(MODEL_ENV, "")).strip()
        if not self.model:
            raise ValueError(f"{MODEL_ENV} is not set")
        self._key = key if key is not None else api_key()
        if not self._key:
            raise ValueError(f"the LLM needs one of {', '.join(KEY_ENVS)}")
        self.url = url or os.environ.get(URL_ENV) or DEFAULT_URL
        self.timeout = timeout
        self.max_retries = max_retries
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.log: list[dict] = []
        self._json_format_ok = True
        effort = os.environ.get(EFFORT_ENV, DEFAULT_EFFORT).strip().lower()
        self._effort = None if effort in ("", "none") else effort

    @property
    def tokens(self) -> int:
        """Input plus output tokens used so far."""
        return self.input_tokens + self.output_tokens

    def _post(self, body: dict) -> dict:
        """POST `body` and return the JSON reply, retrying connection errors, timeouts, 408/409/429 and 5xx with
        backoff, and dropping the reasoning or structured-output parameter when the model rejects it."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(
                    self.url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                detail = resp.text[:500]
                if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                    last_exc = RuntimeError(f"LLM HTTP {resp.status_code}: {detail}")
                elif resp.status_code == 400 and "reasoning" in body and "reasoning" in detail.lower():
                    # not a reasoning model: retry without the effort parameter
                    self._effort = None
                    body = {k: v for k, v in body.items() if k != "reasoning"}
                    continue
                elif resp.status_code == 400 and "text" in body and "format" in detail.lower():
                    # structured output not supported for this model: retry once without it
                    self._json_format_ok = False
                    body = {k: v for k, v in body.items() if k != "text"}
                    continue
                else:
                    raise RuntimeError(f"LLM HTTP {resp.status_code}: {detail}")
            if attempt < self.max_retries:
                time.sleep(min(8.0, 2.0**attempt))
        raise RuntimeError(f"LLM request failed after {self.max_retries + 1} attempts: {last_exc}")

    def complete(
        self, system: str, user: str, *, json_mode: bool = False, max_output_tokens: int = 2000
    ) -> LLMResponse:
        """Ask the model once and return its answer; with `json_mode` the answer must be a JSON object."""
        body: dict[str, Any] = {
            "model": self.model,
            "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_output_tokens": max_output_tokens,
        }
        if self._effort:
            body["reasoning"] = {"effort": self._effort}
        if json_mode and self._json_format_ok:
            body["text"] = {"format": {"type": "json_object"}}
        started = time.monotonic()
        self.calls += 1
        try:
            data = self._post(body)
        except Exception as exc:
            self.log.append({"error": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)})
            raise
        usage = data.get("usage") or {}
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        out = LLMResponse(
            text=output_text(data),
            usage={"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")},
            model=str(data.get("model") or self.model),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if not out.text.strip() and data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            self.log.append({"usage": out.usage, "latency_ms": out.latency_ms, "error": f"incomplete: {reason}"})
            raise RuntimeError(f"LLM answer incomplete ({reason}) with no text")
        if json_mode:
            out.data = parse_json_object(out.text)
        self.log.append({"usage": out.usage, "latency_ms": out.latency_ms, "model": out.model})
        return out

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 3000) -> LLMResponse:
        """Ask the model for a JSON object (see `complete`)."""
        return self.complete(system, user, json_mode=True, max_output_tokens=max_output_tokens)


class CodexLLMClient:
    """Same interface as LLMClient, answered by `codex exec` on the ChatGPT subscription."""

    def __init__(self, model: str | None = None, *, timeout: float = 300.0, **_: Any) -> None:
        """Configure the codex CLI client for `model`; ValueError when no model is set or codex is not installed."""
        self.model = (model or os.environ.get(MODEL_ENV, "")).strip()
        if not self.model:
            raise ValueError(f"{MODEL_ENV} is not set")
        if shutil.which("codex") is None:
            raise ValueError("codex CLI not found (the agent image installs it via install-codex.sh)")
        self.timeout = timeout
        self.calls = 0
        self.input_tokens = 0  # not reported by codex exec
        self.output_tokens = 0
        self.log: list[dict] = []
        effort = os.environ.get(EFFORT_ENV, DEFAULT_EFFORT).strip().lower()
        self._effort = None if effort in ("", "none") else effort

    @property
    def tokens(self) -> int:
        """Tokens used so far (codex exec does not report usage here, so 0)."""
        return 0

    def complete(
        self, system: str, user: str, *, json_mode: bool = False, max_output_tokens: int = 2000
    ) -> LLMResponse:
        """Ask once through `codex exec` and return its answer; with `json_mode` the answer must be a JSON object."""
        del max_output_tokens  # codex exec has no output cap flag
        from llm_backend.judge_bridge import _run_codex

        prompt = f"[system]\n{system}\n\n[user]\n{user}"
        if json_mode:
            prompt += "\n\nReply with one JSON object only, no prose and no code fences."
        started = time.monotonic()
        self.calls += 1
        try:
            with tempfile.TemporaryDirectory(prefix="jev-agent-codex-") as tmp:
                text = _run_codex(prompt, self.model, Path(tmp), os.environ.copy(), self._effort)
        except Exception as exc:
            self.log.append({"error": str(exc)[:500], "latency_ms": int((time.monotonic() - started) * 1000)})
            raise
        out = LLMResponse(text=text, model=self.model, latency_ms=int((time.monotonic() - started) * 1000))
        if not out.text.strip():
            self.log.append({"latency_ms": out.latency_ms, "error": "empty answer"})
            raise RuntimeError("codex returned no text")
        if json_mode:
            out.data = parse_json_object(out.text)
        self.log.append({"latency_ms": out.latency_ms, "model": out.model, "backend": "codex"})
        return out

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 3000) -> LLMResponse:
        """Ask for a JSON object (see `complete`)."""
        return self.complete(system, user, json_mode=True, max_output_tokens=max_output_tokens)


def make_llm(model: str | None = None, **kwargs: Any) -> LLMClient | CodexLLMClient | None:
    """The configured LLM client (codex subscription by default), or None when disabled/unavailable (logged)."""
    model = (model if model is not None else os.environ.get(MODEL_ENV, "")).strip()
    if not model:
        return None
    try:
        if backend() == "codex":
            return CodexLLMClient(model, **kwargs)
        return LLMClient(model, **kwargs)
    except ValueError as exc:
        logger.warning("LLM disabled: %s", exc)
        return None


def ping(model: str | None = None) -> LLMResponse:
    """One tiny call, for preflight."""
    client = (
        CodexLLMClient(model, timeout=120.0) if backend() == "codex" else LLMClient(model, timeout=30.0, max_retries=1)
    )
    return client.complete_json("Reply with a JSON object only.", 'Return {"ok": true}.', max_output_tokens=400)
