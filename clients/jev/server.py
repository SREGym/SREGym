"""A bounded, read-only MCP adapter for the TypeSafe System One API."""

import argparse
import asyncio
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from clients.jev.config import KEY_ENV, MODEL_ENV

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
CALL_TIMEOUT = 30
MAX_CALLS = 40
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 131_072
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str = Field(min_length=1, max_length=8000)


class Choice(Question):
    type: Literal["choice"]
    criteria: dict[str, str | None] = Field(min_length=2, max_length=20)


class Noul(Question):
    type: Literal["noul"]
    criteria: dict[Literal["true", "false"], str] | None = None


class Score(Question):
    type: Literal["score"]
    criteria: list[str] = Field(min_length=2, max_length=20)


TypedQuestion = Annotated[Choice | Noul | Score, Field(discriminator="type")]
Questions = Annotated[dict[str, TypedQuestion], Field(min_length=1, max_length=16)]
QUESTIONS = TypeAdapter(Questions)
State = str | dict[str, JsonValue] | list[JsonValue]
STATE = TypeAdapter(State)


class Usage(BaseModel):
    model_config = ConfigDict(strict=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


def _number(value: object, low: float, high: float) -> bool:
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def validate_response(data: object, questions: dict[str, TypedQuestion]) -> dict:
    """Reject incomplete or mismatched advice rather than treating it as a result."""
    if not isinstance(data, dict) or not isinstance(data.get("model"), str) or not data["model"]:
        raise ValueError("Missing response model")
    answers = data.get("answers")
    if not isinstance(answers, dict) or answers.keys() != questions.keys():
        raise ValueError("Response questions do not match request")
    clean = {}
    for name, question in questions.items():
        answer = answers[name]
        if not isinstance(answer, dict) or answer.get("type") != question.type:
            raise ValueError("Response type does not match question")
        result = {"type": question.type}
        if isinstance(question, Noul):
            if not _number(answer.get("noul"), 0, 1):
                raise ValueError("Invalid noul")
            result["noul"] = answer["noul"]
        else:
            if not _number(answer.get("confidence"), 0, 1):
                raise ValueError("Invalid confidence")
            result["confidence"] = answer["confidence"]
            options = (
                set(question.criteria)
                if isinstance(question, Choice)
                else {str(i) for i in range(len(question.criteria))}
            )
            if isinstance(question, Choice):
                if answer.get("choice") not in options:
                    raise ValueError("Unknown choice")
                result["choice"] = answer["choice"]
            else:
                if not _number(answer.get("score"), 0, len(question.criteria) - 1):
                    raise ValueError("Invalid score")
                result["score"] = answer["score"]
                result["legend"] = {str(i): value for i, value in enumerate(question.criteria)}
            probabilities = answer.get("probabilities")
            # The quickstart also documents Score responses without a distribution.
            if isinstance(question, Choice) or probabilities is not None:
                if (
                    not isinstance(probabilities, dict)
                    or probabilities.keys() != options
                    or not all(_number(p, 0, 1) for p in probabilities.values())
                    or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.02)
                ):
                    raise ValueError("Invalid probability distribution")
                result["probabilities"] = probabilities
        clean[name] = result
    return {"model": data["model"], "answers": clean, "usage": Usage.model_validate(data.get("usage")).model_dump()}


class JevEvaluator:
    def __init__(self, model: str, api_key: str, log_path: Path, *, transport=None):
        if not model.strip() or not api_key.strip():
            raise ValueError("Jev requires a model and TYPESAFE_API_KEY")
        self.model = model
        self.api_key = api_key
        self.log_path = log_path
        self.transport = transport
        self.calls = 0
        self.lock = asyncio.Lock()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # A failed audit path must prevent startup, not silently lose all call records.
        with log_path.open("a", encoding="utf-8"):
            pass

    def record(self, record: dict) -> None:
        text = json.dumps(record, ensure_ascii=False, allow_nan=False)
        text = text.replace(self.api_key, "[REDACTED]")
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")

    async def evaluate(self, state: JsonValue, questions: dict) -> dict:
        start = time.monotonic()
        record = {"timestamp": datetime.now(UTC).isoformat(), "requested_model": self.model}
        try:
            state = STATE.validate_python(state)
            typed = QUESTIONS.validate_python(questions)
            body = {
                "state": state,
                "model": self.model,
                "questions": {k: v.model_dump(exclude_none=True) for k, v in typed.items()},
            }
            encoded = json.dumps(body, allow_nan=False).encode()
            if len(encoded) > MAX_REQUEST_BYTES:
                raise ValueError("Request exceeds 64 KiB")
            if self.api_key in encoded.decode():
                raise ValueError("Do not include API credentials in evidence")
        except (ValidationError, ValueError, TypeError):
            result = {
                "error": "invalid_request",
                "message": "Use valid typed questions and at most 64 KiB of evidence. Do not include credentials.",
            }
        else:
            record["request"] = body
            # Include the queue in the deadline. Parallel tool calls cannot extend it.
            try:
                async with asyncio.timeout(CALL_TIMEOUT):
                    async with self.lock:
                        if self.calls >= MAX_CALLS:
                            result = {
                                "error": "call_budget_exhausted",
                                "message": f"The {MAX_CALLS}-call Jev budget is exhausted. Continue without Jev.",
                            }
                        else:
                            self.calls += 1
                            record["call_number"] = self.calls
                            result = await self._request(body, typed, record)
            except TimeoutError:
                result = {
                    "error": "timeout",
                    "message": "Jev exceeded the 30-second call deadline. Continue without this advice.",
                }
        result["elapsed_seconds"] = round(time.monotonic() - start, 3)
        record["result"] = result
        self.record(record)
        return result

    async def _request(self, body: dict, questions: dict, record: dict) -> dict:
        # trust_env preserves the benchmark's proxy and CA configuration. No
        # redirects: this credential must only reach the configured TypeSafe API.
        async with httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False) as client:
            for attempt in range(2):
                record["http_attempts"] = attempt + 1
                try:
                    async with client.stream(
                        "POST", ENDPOINT, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
                    ) as response:
                        status = response.status_code
                        if status in RETRY_STATUSES and attempt == 0:
                            try:
                                delay = min(5, max(1, float(response.headers.get("Retry-After", "1"))))
                            except ValueError:
                                delay = 1
                            await asyncio.sleep(delay)
                            continue
                        if status != 200:
                            return {
                                "error": "provider_error",
                                "http_status": status,
                                "message": "Jev did not return advice. Continue without this advice.",
                            }
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_RESPONSE_BYTES:
                                return {"error": "invalid_response", "message": "Jev response exceeded the size limit."}
                    try:
                        return validate_response(json.loads(raw), questions)
                    except (ValueError, TypeError):
                        return {"error": "invalid_response", "message": "Jev returned an incomplete or invalid result."}
                except httpx.TimeoutException:
                    # Do not retry an ambiguous timeout: the provider may have billed it.
                    return {"error": "timeout", "message": "Jev request timed out. Continue without this advice."}
                except httpx.RequestError:
                    return {"error": "connection_error", "message": "Cannot reach Jev. Continue without this advice."}
        raise AssertionError("Unreachable retry state")


def create_server(evaluator: JevEvaluator) -> FastMCP:
    server = FastMCP("jev")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    async def jev_evaluate(state: State, questions: Questions) -> dict:
        """Evaluate evidence with Jev; returns judgments, not explanations or commands.

        Supply observed facts as state. Ask narrow Choice, Noul, or Score questions.
        Choice compares candidate decisions; include insufficient evidence when relevant.
        Noul estimates whether a claim is true. Score rates one dimension on an ordered rubric.
        Jev sees only this request, not your session or the cluster. Do not send credentials.
        Results are advice, not proof. Limits: 16 questions, 64 KiB, 40 calls per attempt.
        """
        return await evaluator.evaluate(state, questions)

    return server


def run_preflight(log_path: Path) -> None:
    """Verify the Jev credential and response contract before fault deployment."""
    evaluator = JevEvaluator(os.environ.get(MODEL_ENV, ""), os.environ.get(KEY_ENV, ""), log_path)
    result = asyncio.run(
        evaluator.evaluate(
            {"status": "Ready"},
            {"ready": {"type": "noul", "instructions": "Is the observed status Ready?"}},
        )
    )
    if "error" in result:
        raise RuntimeError(f"Jev preflight failed: {result['error']} (HTTP {result.get('http_status', 'n/a')})")
    print(f"Jev preflight passed: model={result['model']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-path", type=Path, required=True)
    args = parser.parse_args()
    evaluator = JevEvaluator(os.environ.get(MODEL_ENV, ""), os.environ.get(KEY_ENV, ""), args.log_path)
    create_server(evaluator).run(transport="stdio")


if __name__ == "__main__":
    main()
