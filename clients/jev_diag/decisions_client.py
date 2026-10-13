"""OpenAI Decisions API backend with the slice of TypeSafeClient that jev_diag uses (`system_one`).

Selected with JEV_DIAG_BACKEND=openai_decisions. Jev questions map onto Decisions questions one to one:
a Choice becomes a `choice` question (criteria keys are the values, criteria texts the descriptions) and a
Noul becomes a `predicate` whose probability is P(true). The structured state is sent as compact JSON text.
Answers come back as typesafe_sdk structs, so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

import msgspec
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer, SystemOneResponse, Usage

BACKEND_ENV = "JEV_DIAG_BACKEND"
BACKEND_NAME = "openai_decisions"
OPENROUTER_BACKEND = "openrouter"
OPENROUTER_KEY_ENV = "DECISIONS_OPENROUTER_API_KEY"
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_DEFAULT_MODEL = "microsoft/microsoft-decision-1"
KEY_ENV = "DECISIONS_OPENAI_API_KEY"  # not OPENAI_API_KEY, so a subscription judge never picks it up
MODEL_ENV = "DECISIONS_MODEL"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_URL = "https://api.openai.com/v1/decisions"


def backend() -> str:
    """The configured decision backend (JEV_DIAG_BACKEND), empty for the default Jev client."""
    return os.getenv(BACKEND_ENV, "").strip()


def enabled() -> bool:
    """Whether a Decisions backend (OpenAI or OpenRouter) is selected."""
    return backend() in (BACKEND_NAME, OPENROUTER_BACKEND)


def make_client(timeout: float = 90.0):
    """A Decisions client for the selected backend."""
    if backend() == OPENROUTER_BACKEND:
        return OpenRouterClient(timeout=timeout)
    return DecisionsClient(timeout=timeout)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(msgspec.to_builtins(value), ensure_ascii=False, indent=1)


def to_wire_question(name: str, question: Any) -> dict:
    """A Jev question (Choice or Noul) as a Decisions API question."""
    criteria = dict(msgspec.to_builtins(question.criteria) or {})
    instructions = _text(question.instructions)
    if isinstance(question, Choice):
        return {
            "name": name,
            "type": "choice",
            "instructions": instructions,
            "choices": [{"value": str(value), "description": _text(desc)} for value, desc in criteria.items()],
        }
    if isinstance(question, Noul):
        parts = [instructions]
        if criteria.get("true"):
            parts.append(f"True means: {_text(criteria['true'])}")
        if criteria.get("false"):
            parts.append(f"False means: {_text(criteria['false'])}")
        return {"name": name, "type": "predicate", "instructions": "\n".join(parts)}
    raise TypeError(f"unsupported question type for {name}: {type(question).__name__}")


def from_wire_answers(body: dict) -> SystemOneResponse:
    """Decisions API answers as a typesafe_sdk response (choice probabilities, predicate P(true), usage)."""
    answers: dict[str, Any] = {}
    for item in body.get("answers") or []:
        name, kind = item.get("name"), item.get("type")
        if kind == "choice":
            probs = {str(p["value"]): float(p["probability"]) for p in item.get("probabilities") or []}
            answers[name] = ChoiceAnswer(
                choice=str(item["choice"]), confidence=float(item.get("confidence") or 0.0), probabilities=probs
            )
        elif kind == "predicate":
            answers[name] = NoulAnswer(noul=float(item["probability"]))
        # refusals and unknown types are dropped; callers already treat a missing noul as unknown
    usage = body.get("usage") or {}
    return SystemOneResponse(
        model=body.get("model") or "",
        usage=Usage(input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens")),
        answers=answers,
    )


class DecisionsClient:
    """Decisions API client with the `system_one` call that jev_diag makes on a Jev client, with retries."""

    def __init__(self, model: str | None = None, *, timeout: float = 90.0, max_retries: int = 3) -> None:
        self.model = model or os.getenv(MODEL_ENV) or DEFAULT_MODEL
        self.url = os.getenv("DECISIONS_URL", DEFAULT_URL)
        self.timeout = timeout
        self.max_retries = max_retries
        self._key = os.getenv(KEY_ENV, "").strip()
        if not self._key:
            raise ValueError(f"{BACKEND_NAME} backend requires {KEY_ENV}")

    def _body(self, state: Any, questions: dict[str, Any]) -> dict:
        return {
            "model": self.model,
            "input": json.dumps(msgspec.to_builtins(state), ensure_ascii=False, separators=(",", ":")),
            "questions": [to_wire_question(name, q) for name, q in questions.items()],
        }

    def _parse(self, body: dict) -> SystemOneResponse:
        return from_wire_answers(body)

    def system_one(self, state: Any, questions: dict[str, Any], **_: Any) -> SystemOneResponse:
        """Ask all `questions` about `state` in one request and return the answers."""
        data = json.dumps(self._body(state, questions)).encode()
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                self.url,
                data=data,
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return self._parse(json.loads(response.read()))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:1000]
                if exc.code in (408, 409, 429) or exc.code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(min(10.0, 2.0**attempt))
                        continue
                raise RuntimeError(f"Decisions API HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError):
                if attempt < self.max_retries:
                    time.sleep(min(10.0, 2.0**attempt))
                    continue
                raise
        raise RuntimeError("unreachable")


def from_jev_wire(body: dict) -> SystemOneResponse:
    """OpenRouter answers use Jev's wire shape: answers keyed by question name, typed noul/choice."""
    answers: dict[str, Any] = {}
    for name, item in (body.get("answers") or {}).items():
        kind = item.get("type")
        if kind == "choice":
            probs = {str(k): float(v) for k, v in (item.get("probabilities") or {}).items()}
            answers[name] = ChoiceAnswer(
                choice=str(item["choice"]), confidence=float(item.get("confidence") or 0.0), probabilities=probs
            )
        elif kind == "noul":
            answers[name] = NoulAnswer(noul=float(item["noul"]))
    usage = body.get("usage") or {}
    return SystemOneResponse(
        model=body.get("model") or "",
        usage=Usage(input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens")),
        answers=answers,
    )


class OpenRouterClient(DecisionsClient):
    """OpenRouter Decisions endpoint (e.g. microsoft/microsoft-decision-1). Questions go over unchanged."""

    def __init__(self, model: str | None = None, *, timeout: float = 90.0, max_retries: int = 3) -> None:
        self.model = model or os.getenv(MODEL_ENV) or OPENROUTER_DEFAULT_MODEL
        self.url = os.getenv("DECISIONS_URL", OPENROUTER_URL)
        self.timeout = timeout
        self.max_retries = max_retries
        self._key = os.getenv(OPENROUTER_KEY_ENV, "").strip()
        if not self._key:
            raise ValueError(f"{OPENROUTER_BACKEND} backend requires {OPENROUTER_KEY_ENV}")

    def _body(self, state: Any, questions: dict[str, Any]) -> dict:
        return {
            "model": self.model,
            "state": msgspec.to_builtins(state),
            "questions": {name: msgspec.to_builtins(q) for name, q in questions.items()},
        }

    def _parse(self, body: dict) -> SystemOneResponse:
        return from_jev_wire(body)
