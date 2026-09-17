"""A bounded decision-support MCP adapter for the TypeSafe System One API."""

import argparse
import asyncio
import json
import math
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from clients.jev.config import KEY_ENV, MODEL_ENV
from clients.jev.evidence import Observation, evidence_guidance, evidence_questions
from clients.jev.observations import collect_observations
from clients.jev.planning import DiagnosticTest, planning_guidance, planning_questions
from clients.jev.review import collect_snapshot, review_guidance, review_questions
from clients.jev.submission import SubmissionClient

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
    criteria: dict[str, str | None] = Field(min_length=2, max_length=64)


class Noul(Question):
    type: Literal["noul"]
    criteria: dict[Literal["true", "false"], str] | None = None


class Score(Question):
    type: Literal["score"]
    criteria: list[str] = Field(min_length=2, max_length=10)


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


def create_server(evaluator: JevEvaluator, *, submitter: SubmissionClient | None = None) -> FastMCP:
    server = FastMCP("jev")
    submitter = submitter or SubmissionClient()
    replan_required: set[str] = set()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    async def jev_observe(namespace: str) -> dict:
        """Collect and prioritize fresh Pod observations before choosing a cause.

        Reads current Pod status and bounded logs from the last 60 seconds using
        your existing Kubernetes connection. Obvious credential lines are omitted.
        Jev classifies each source independently of your preferred explanation.
        It does not read Secrets, benchmark files, or private expected answers.
        It makes no changes. Its ranking is a lead, not a proven diagnosis.
        Inspect the actual output, reproduce the failed application operation,
        and map any failing address to live configuration before making a repair.
        Absence of errors is not proof that all application operations work.
        """
        try:
            observations = await collect_observations(namespace)
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
            return {
                "error": "observations_unavailable",
                "message": "Could not collect bounded observations. Use your normal read-only diagnostic tools.",
            }
        rows = []
        for offset in range(0, len(observations), 8):
            batch = observations[offset : offset + 8]
            result = await evaluator.evaluate(
                {f"observation_{i}": item.model_dump() for i, item in enumerate(batch, 1)},
                evidence_questions(batch),
            )
            if "error" in result:
                return {
                    "error": result["error"],
                    "message": "Jev classification was incomplete. Do not treat missing sources as healthy. Continue with normal diagnostic tools.",
                }
            for row in evidence_guidance(result, batch)["observations"]:
                row["id"] = f"observation_{offset + int(row['id'].removeprefix('observation_'))}"
                rows.append(row)
        rows.sort(key=lambda row: row["priority"], reverse=True)
        active = [row for row in rows if row["category"] == "application_failure"]
        result = {
            "active_failure_candidates": active,
            "other_observations": [row for row in rows if row["category"] != "application_failure"],
            "next_step": "Start from the actual output of an active failure candidate, not a historical error or resource-size guess. Reproduce that operation and inspect its exact dependencies. A category is not a diagnosis. If there are no active candidates, test application operations directly; missing log errors do not establish recovery.",
        }
        evaluator.record({"timestamp": datetime.now(UTC).isoformat(), "tool": "jev_observe", "result": result})
        return result

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    async def jev_triage(
        observations: Annotated[list[Observation], Field(min_length=3, max_length=8)],
    ) -> dict:
        """Separate active application failures from noise using verbatim evidence.

        Supply 3-8 different observations, including fresh request results, log
        excerpts, healthy controls, and evidence against your current suspicion.
        Each needs its command or source, collection time, and actual output.
        Do not replace output with your diagnosis. Omit credentials and secrets.
        This tool reads no files, runs no commands, and makes no resource changes.
        Jev classifies and prioritizes evidence; it does not identify a known fault.
        Read the ranked evidence, then reproduce the specific failing operation.
        """
        result = await evaluator.evaluate(
            {f"observation_{i}": item.model_dump() for i, item in enumerate(observations, 1)},
            evidence_questions(observations),
        )
        guidance = evidence_guidance(result, observations)
        evaluator.record({"timestamp": datetime.now(UTC).isoformat(), "tool": "jev_triage", "guidance": guidance})
        return {"guidance": guidance, **result}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    async def jev_plan(
        namespace: str,
        observations: State,
        tests: Annotated[list[DiagnosticTest], Field(min_length=3, max_length=5)],
    ) -> dict:
        """Choose the next diagnostic test from competing explanations.

        Supply 3-5 different hypotheses, each with a read-only command and
        contrasting expected outcomes. Include relevant contrary evidence.
        This tool collects the current namespace snapshot and ranks tests with
        Jev Choice and Score questions. It does not run commands or make repairs.
        Read the selected tests, check their safety, then execute them separately.
        Use their actual outputs to reject hypotheses before requesting a review.
        Do not include credentials, or only propose variants of one hypothesis.
        """
        try:
            snapshot = await collect_snapshot(namespace)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return {"error": "snapshot_unavailable", "message": "Cannot collect the namespace snapshot."}
        result = await evaluator.evaluate(
            {
                "direct_snapshot": snapshot,
                "observations": observations,
                "candidate_tests": {f"test_{i}": test.model_dump() for i, test in enumerate(tests, 1)},
            },
            planning_questions(tests),
        )
        guidance = planning_guidance(result, tests)
        if guidance.get("tests"):
            replan_required.discard(namespace)
        evaluator.record({"timestamp": datetime.now(UTC).isoformat(), "tool": "jev_plan", "guidance": guidance})
        return {"guidance": guidance, **result}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    async def jev_review(
        namespace: str,
        phase: Literal["investigate", "diagnose", "mitigate", "verify"],
        observations: State,
        hypothesis: str = "",
        proposed_action: str = "",
    ) -> dict:
        """Review an SRE decision with direct Kubernetes evidence and fixed questions.

        Call early during investigation and before diagnosis, repair, or recovery claims.
        Supply fresh command output and symptoms, including contradictory evidence.
        This tool reads workloads, Pods, Services, endpoints, PVCs, and network policies.
        It uses the same Kubernetes credentials and proxy as your kubectl commands.
        It does not read Secrets, environment values, logs, annotations, or benchmark files.
        Investigate ranks the next area/component to inspect and evaluates causal support.
        Other phases return evidence judgments only, without candidate rankings.
        Diagnose/mitigate also check for a demonstrated recent application failure.
        Mitigate/verify also assess durable repair and functional evidence.
        Rankings are investigation suggestions, not proven causes or commands to apply.
        Low or uncertain support calls for a discriminating test, not a submission.
        Read the response before your next action. Do not include credentials.
        """
        try:
            snapshot = await collect_snapshot(namespace)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            result = {
                "error": "snapshot_unavailable",
                "message": "Cannot collect the bounded namespace snapshot. Continue with explicit evidence using jev_evaluate.",
            }
            evaluator.record({"timestamp": datetime.now(UTC).isoformat(), "tool": "jev_review", "result": result})
            return result
        result = await evaluator.evaluate(
            {
                "phase": phase,
                "direct_snapshot": snapshot,
                "agent_observations": observations,
                "agent_hypothesis": hypothesis,
                "proposed_action": proposed_action,
            },
            review_questions(snapshot, phase),
        )
        guidance = review_guidance(result, phase)
        evaluator.record({"timestamp": datetime.now(UTC).isoformat(), "tool": "jev_review", "guidance": guidance})
        return {"guidance": guidance, **result}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    async def jev_submit(
        namespace: str,
        stage: Literal["diagnosis", "mitigation"],
        observations: State,
        diagnosis: str,
        applied_action: str = "",
    ) -> dict:
        """Review evidence, then submit to the Conductor only if it has support.

        Use this instead of curl for diagnosis and mitigation submissions.
        Diagnosis must describe the causal mechanism, not just a component name.
        For mitigation, include before/after tests and the action actually applied.
        The tool reads a fresh namespace snapshot and uses fixed Jev questions.
        Any required yes-probability below 0.7 returns not_submitted. Gather new
        evidence through a new jev_plan and its selected diagnostic tests before
        retrying. Do not repeat or reword the rejected claim.
        Scores of at least 0.7 permit submission but do not guarantee correctness.
        Only status=accepted means the Conductor accepted this stage.
        This tool never changes Kubernetes resources or the benchmark grading.
        """
        if namespace in replan_required:
            result = {
                "status": "not_submitted",
                "reason": "The previous submission review was rejected. Call jev_plan with competing explanations, run the selected diagnostic tests, and supply new observations before retrying. Rewording the rejected claim does not resolve it.",
                "next_tool": "jev_plan",
            }
            evaluator.record(
                {"timestamp": datetime.now(UTC).isoformat(), "tool": "jev_submit", "stage": stage, "result": result}
            )
            return result
        if not diagnosis.strip() or (stage == "mitigation" and not applied_action.strip()):
            return {"status": "not_submitted", "reason": "Supply a diagnosis and, for mitigation, the applied action."}
        review = await jev_review(
            namespace, "diagnose" if stage == "diagnosis" else "verify", observations, diagnosis, applied_action
        )
        guidance = review.get("guidance", {})
        if guidance.get("assessment") in {"unsupported", "uncertain"}:
            replan_required.add(namespace)
        if guidance.get("assessment") != "supported":
            result = {
                "status": "not_submitted",
                "reason": "The review does not support submission. Resolve the weak claims with new observations.",
                "next_tool": "jev_plan" if namespace in replan_required else "jev_submit",
                "review": review,
            }
        else:
            result = {**await submitter.submit(stage, diagnosis if stage == "diagnosis" else ""), "review": review}
        evaluator.record(
            {"timestamp": datetime.now(UTC).isoformat(), "tool": "jev_submit", "stage": stage, "result": result}
        )
        return result

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    async def jev_evaluate(state: State, questions: Questions) -> dict:
        """Evaluate evidence with Jev; returns judgments, not explanations or commands.

        Use a state object with separate observations, hypotheses, and unknowns.
        Include relevant evidence against each hypothesis, not just evidence in its favor.
        Jev sees only this request, not your session or the cluster. Do not send credentials.

        Choice compares alternatives with neutral descriptions. Include insufficient evidence.
        Read the full probability distribution, not just the selected choice.
        Noul returns the probability of yes for one factual claim. Near 0.5 means uncertainty.
        Noul has no separate confidence value. Ask separate questions for separate claims.
        Score uses 2-10 ordered, descriptive levels for one dimension, not bare numbers.
        Its score is a fractional position from 0 to N-1. Use the returned legend.
        Choice/Score confidence describes distribution concentration, not guaranteed correctness.

        Call this tool separately from commands or submissions. Read its response before acting.
        If the evidence does not distinguish alternatives, gather more evidence before a change.
        High confidence cannot compensate for missing evidence or an incorrect premise.
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
