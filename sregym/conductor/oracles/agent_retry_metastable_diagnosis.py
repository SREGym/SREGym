"""Diagnosis oracle for agent retry metastable overload evaluating causal concepts."""

from __future__ import annotations

import logging
import re
from typing import Any

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

logger = logging.getLogger("all.conductor.oracle.agent_retry_diagnosis")


class AgentRetryMetastableDiagnosisOracle(Oracle):
    """Evaluates agent diagnosis against 4 required causal dimensions:

    1. Localization: agent/tool -> backend request path
    2. Trigger: temporary / transient backend latency spike
    3. Sustaining mechanism: stacked / multi-layer retries across planner, tool, transport
    4. Impact: backend concurrency/queue saturation causing persistent/metastable overload
    """

    FAILURE_CLASSES = {
        "diagnosis_incomplete": FailureClass.AGENT_ERROR,
        "missing_localization": FailureClass.AGENT_ERROR,
        "missing_trigger": FailureClass.AGENT_ERROR,
        "missing_sustaining_mechanism": FailureClass.AGENT_ERROR,
        "missing_impact": FailureClass.AGENT_ERROR,
    }

    DIMENSIONS = {
        "localization": {
            "name": "agent/tool -> backend request path",
            "patterns": [
                r"\b(agent|workflow|supervisor|planner)\b.*\b(tool|backend|service|db|database|storage)\b",
                r"\b(tool|client|http|grpc)\b.*\b(backend|service|postgres|db)\b",
                r"\b(request path|call chain|end-to-end)\b",
            ],
            "keywords": ["agent", "tool", "backend", "planner", "workflow"],
        },
        "trigger": {
            "name": "temporary / transient backend latency spike",
            "patterns": [
                r"\b(transient|temporary|short-lived|brief|initial|spike)\b.*\b(latency|slowdown|delay|timeout)\b",
                r"\b(latency|slowdown|delay)\b.*\b(transient|temporary|spike|removed|disappear)\b",
            ],
            "keywords": ["transient", "temporary", "latency", "slowdown", "delay", "spike"],
        },
        "sustaining_mechanism": {
            "name": "speculative replanning, uncancelled orphaned work, and nested retries",
            "patterns": [
                r"\b(stacked|nested|independent|multi-layer|compound|amplif\w+)\b.*\b(retr\w+|replan\w+)\b",
                r"\b(retr\w+)\b.*\b(planner|workflow|tool|transport|client)\b",
                r"\b(retry storm|retry amplification|amplified requests)\b",
                r"\b(orphan\w*|uncancel\w*|speculative|replacement)\b.*\b(work|task|operation|query|replan\w*)\b",
            ],
            "keywords": [
                "retry",
                "retries",
                "replanning",
                "replan",
                "amplification",
                "multi-layer",
                "stacked",
                "orphaned",
                "orphan",
                "uncancelled",
                "speculative",
                "cancellation",
            ],
        },
        "impact": {
            "name": "backend concurrency/queue saturation sustaining metastable overload",
            "patterns": [
                r"\b(concurrency|queue|pool|connection|worker)\b.*\b(saturat\w+|exhaust\w+|overload|full|backlog)\b",
                r"\b(metastable|self-sustaining|persistent|sustained)\b.*\b(overload|degradation|outage|failure)\b",
                r"\b(queue|waiting)\b.*\b(persistent|remain|continue|sustain)\b",
            ],
            "keywords": ["concurrency", "queue", "pool", "saturation", "metastable", "persistent", "worker"],
        },
    }

    def __init__(self, problem, expected: str = ""):
        super().__init__(problem)
        self.expected = expected

    def _score_dimension(self, text: str, dim_key: str) -> tuple[float, str]:
        dim_info = self.DIMENSIONS[dim_key]
        text_lower = text.lower()

        # Check regex patterns
        for pattern in dim_info["patterns"]:
            if re.search(pattern, text_lower):
                return 1.0, f"Matched pattern: {pattern}"

        # Fallback: keyword count
        matched_kw = [kw for kw in dim_info["keywords"] if kw in text_lower]
        if len(matched_kw) >= 2:
            return 0.75, f"Matched keywords: {matched_kw}"
        elif len(matched_kw) == 1:
            return 0.35, f"Partial keyword match: {matched_kw}"

        return 0.0, "Missing dimension concepts"

    def evaluate(self, solution, trace=None, duration=None) -> dict[str, Any]:
        """Evaluate agent diagnosis against required causal dimensions."""
        print("== Agent Retry Metastable Diagnosis Evaluation ==")
        if not solution:
            return self.fail("diagnosis_incomplete", error="Empty solution provided")

        solution_str = str(solution)
        dim_scores = {}
        total_score = 0.0

        for key, dim_info in self.DIMENSIONS.items():
            score, evidence = self._score_dimension(solution_str, key)
            dim_scores[key] = {
                "name": dim_info["name"],
                "score": score,
                "evidence": evidence,
            }
            total_score += score

        composite_score = round(total_score / len(self.DIMENSIONS), 2)
        percentage = round(composite_score * 100.0, 1)

        print(f"[Diagnosis Score] Composite: {percentage}% ({total_score:.2f}/{len(self.DIMENSIONS)})")
        for key, details in dim_scores.items():
            status = "✅" if details["score"] >= 0.75 else ("⚠️" if details["score"] > 0 else "❌")
            print(f"  {status} {key}: score={details['score']:.2f} ({details['evidence']})")

        # Require at least 0.70 composite and no completely missed critical dimension
        passed = (
            composite_score >= 0.70
            and dim_scores["sustaining_mechanism"]["score"] >= 0.5
            and dim_scores["impact"]["score"] >= 0.5
        )

        if passed:
            print("[PASS] Diagnosis correctly identified the multi-layer retry metastable mechanism.")
            return {
                "success": True,
                "accuracy": percentage,
                "composite_score": composite_score,
                "dimensions": dim_scores,
            }

        # Determine primary missing dimension for clear feedback
        for key, details in dim_scores.items():
            if details["score"] < 0.5:
                return {
                    **self.fail(f"missing_{key}", score=percentage, dimension=key, details=details),
                    "accuracy": percentage,
                    "composite_score": composite_score,
                    "dimensions": dim_scores,
                }

        return {
            **self.fail("diagnosis_incomplete", score=percentage, details=dim_scores),
            "accuracy": percentage,
            "composite_score": composite_score,
            "dimensions": dim_scores,
        }
