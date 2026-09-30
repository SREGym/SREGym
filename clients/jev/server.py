"""Codex MCP tools for Jev planning and reviewed submissions."""

import argparse
import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from clients.jev.client import JevEvaluator, State
from clients.jev.config import KEY_ENV, MODEL_ENV
from clients.jev.planning import DiagnosticTest, planning_guidance, planning_questions
from clients.jev.review import collect_snapshot, review_guidance, review_questions
from clients.jev.submission import SubmissionClient


def create_server(evaluator: JevEvaluator, *, submitter: SubmissionClient | None = None) -> FastMCP:
    server = FastMCP("jev")
    submitter = submitter or SubmissionClient()
    replan_required: set[str] = set()

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

    async def review_submission(
        namespace: str,
        phase: Literal["diagnose", "verify"],
        observations: State,
        hypothesis: str = "",
        proposed_action: str = "",
    ) -> dict:
        """Collect fresh evidence and review a proposed stage submission."""
        try:
            snapshot = await collect_snapshot(namespace)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            result = {
                "error": "snapshot_unavailable",
                "message": "Cannot collect the bounded namespace snapshot. Retry after the Kubernetes connection recovers.",
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
            review_questions(phase),
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
        review = await review_submission(
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
