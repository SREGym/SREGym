"""LLM-as-a-Judge Oracle for evaluating agent solutions against expected root causes.

The checklist evaluator compares agent diagnosis text against the ground-truth
root cause in natural language.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from llm_backend.init_backend import get_llm_backend_for_judge

# ---------------------------------------------------------------------------
# Exceptions and enums
# ---------------------------------------------------------------------------


class ChecklistParseError(Exception):
    """Raised when the LLM response cannot be parsed into checklist results."""


class JudgeParseError(Exception):
    """Raised after all retries are exhausted when parsing checklist results."""


# Reasoning models (Gemini 2.5+/3.x, o-series, Claude with extended thinking)
# count their hidden reasoning tokens against ``max_tokens``. A 4096 budget was
# often consumed by reasoning alone, so the visible JSON was cut mid-string and
# every checklist question defaulted to "No" (SREGym-Lite finalizer run: 51.9 s
# judge call, 620-char truncated array, ``finish_reason=length``).
DEFAULT_JUDGE_MAX_TOKENS = 16384
# When the provider reports ``finish_reason=length`` the retry grows the budget
# by this factor instead of repeating the same request.
_TRUNCATION_RETRY_FACTOR = 4


class JudgmentResult(StrEnum):
    TRUE = "True"  # Correct diagnosis - agent identified the root cause
    FALSE = "False"  # Incorrect diagnosis - agent did not identify the root cause


class LLMJudge:
    def __init__(
        self,
        provider: str | None = None,
        model_name: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS,
    ):
        self.provider = provider
        self.model_name = model_name
        self.url = url
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._backend = None

    @property
    def backend(self):
        """Lazily initialize the LLM backend only when needed."""
        if self._backend is None:
            try:
                self._backend = get_llm_backend_for_judge(
                    provider=self.provider,
                    model_name=self.model_name,
                    api_base=self.url,
                    api_key=self.api_key,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except (SystemExit, Exception) as e:
                print(f"Warning: Failed to initialize LLM backend for judge: {e}")
                print("Returning None - evaluation will be skipped")
                return None
        return self._backend

    def judge(self, solution: str, expectation: str) -> tuple[JudgmentResult, str]:
        if self.backend is None:
            return None, "LLM judge backend is not initialized - skipping evaluation"

        system_prompt = (
            "You are an expert judge evaluating whether an agent's diagnosis of a system "
            "issue matches the expected root cause.\n\n"
            "Your task is to compare the agent's answer with the expected root cause and "
            "determine if they are semantically equivalent.\n\n"
            "Classification criteria:\n"
            "- **True**: The agent correctly identified the root cause.\n"
            "- **False**: The agent did not identify the root cause.\n\n"
            "Respond with EXACTLY this JSON:\n"
            '{"judgment": "True|False", "reasoning": "..."}'
        )

        user_prompt = (
            f"Expected Root Cause:\n"
            f"{expectation if expectation else '(No fault - system is operating normally)'}\n\n"
            f"Agent's Answer:\n{solution}\n\n"
            "Evaluate whether the agent's answer correctly identifies the root cause."
        )

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        try:
            response = self.backend.inference(messages)
            return self._parse_judgment(self._content_text(response.content))
        except Exception as e:
            print(f"Error during judgment: {e}")
            raise

    @staticmethod
    def _content_text(content: object) -> str:
        """Normalize LangChain string or multimodal content blocks to text."""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    value = block.get("text") or block.get("content")
                    if value is not None:
                        parts.append(str(value))
                else:
                    value = getattr(block, "text", None)
                    if value is not None:
                        parts.append(str(value))
            return "".join(parts).strip()
        return str(content or "").strip()

    def _parse_judgment(self, response_text: str) -> tuple[JudgmentResult, str]:
        reasoning = ""
        try:
            clean_text = re.sub(r"```json\s*|\s*```", "", response_text).strip()
            response_json = json.loads(clean_text)
            judgment_str = response_json.get("judgment", "").strip()
            reasoning = response_json.get("reasoning", "")
        except json.JSONDecodeError:
            judgment_str = response_text
            reasoning = "Failed to parse structured response"

        judgment_str = judgment_str.strip().lower()
        if judgment_str == "true":
            return JudgmentResult.TRUE, reasoning
        elif judgment_str == "false":
            return JudgmentResult.FALSE, reasoning
        else:
            raise ValueError(f"Could not parse judgment from response: {response_text}")


# ===================================================================
# DiagnosisJudge  (checklist-based, multi-dimensional, CoT)
# ===================================================================


class DiagnosisJudge:
    """Checklist-based RCA evaluator that scores dimensions via chain-of-thought.

    Uses chain-of-thought reasoning with in-context examples per dimension.
    """

    # ------------------------------------------------------------------
    # System prompt: chain-of-thought + in-context examples
    # ------------------------------------------------------------------
    _SYSTEM_PROMPT_TEMPLATE = """\
You are an expert SRE evaluator assessing an AI agent's root cause analysis.

You will be given:
    1. The **ground-truth root cause** in natural language.
    2. The agent's **diagnosis** (what the agent claims happened).
    3. A checklist of {{num_questions}} Yes/No questions grouped across
     {{num_dimensions}} evaluation dimensions.

## How to evaluate (chain-of-thought)

For EACH dimension, follow these steps **in order**:

  **Step 1 — Understand the dimension.**  Re-read the dimension definition and
  its evaluator hints.

    **Step 2 — Gather evidence.**  Scan the agent's *diagnosis text* for
    claims relevant to each dimension.

    **Step 3 — Compare against ground truth.**  Use the ground-truth root cause
    as the authoritative reference.
  Pay special attention to:
        - the named target component/workload and resource context for localization (D1)
        - concrete root-cause details (misconfig, value mismatch, policy, auth, etc.) for characterization (D2)
        - direct vs secondary impact scope for precision (D3)

  **Step 4 — Answer each question.**  For every question produce:
    - id: the question ID exactly as given
    - answer: exactly "Yes" or "No"
    - evidence: ≤30-word quote or paraphrase supporting your answer
    - confidence: "High", "Medium", or "Low"

## Rules
    - Answer based ONLY on what is explicitly stated in the diagnosis.
  - Do NOT infer information not present in the provided texts.
  - "Yes" always means the positive/correct outcome is present.
  - Treat each question independently.

## In-Context Examples

Below are worked examples showing the expected reasoning depth for each
dimension.  Your actual response must contain ONLY the JSON array; the
chain-of-thought reasoning happens internally before you produce each answer.

### Example — D1 Fault Localization

Ground-truth root cause says: "The checkout deployment has a misconfigured port."
Agent diagnosis says: "The checkout deployment has a misconfigured port."

Reasoning (internal): The agent names "checkout deployment" which matches the
ground-truth component and resource context.
The agent does not blame any other component.  → D1-Q1: Yes, D1-Q2: Yes, D1-Q3: Yes.

### Example — D2 Fault Characterization

Ground-truth root cause says: "checkout cannot reach product-catalog because
PRODUCT_CATALOG_ADDR points to port 8082 instead of 8080."
Agent diagnosis says: "checkout can't reach product-catalog because the
PRODUCT_CATALOG_ADDR env var points to port 8082 instead of 8080."

Reasoning: Specific detail (env var name, wrong port value) matches the root cause ✓.
Diagnosis explains the same configuration fault ✓.
→ D2-Q1: Yes, D2-Q2: Yes, D2-Q3: Yes.

### Example — D3 Scope Precision

Ground-truth root cause says: "frontend has a missing environment variable."
Agent diagnosis says: "frontend, cart, and recommendation services are all
failing because of a missing environment variable on the frontend."

Reasoning: The agent correctly identifies the root cause on frontend but
over-attributes the impact (cart and recommendation are victims, not
direct causes). The underlying fault type matches (misconfiguration).
→ D3-Q1: No (blames extra components), D3-Q2: Yes, D3-Q3: Yes.

---

Respond ONLY with the JSON array of {{num_questions}} objects, no markdown
fences, no preamble, no commentary.
"""

    def __init__(
        self,
        provider: str | None = None,
        model_name: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS,
        checklist_path: str | None = None,
    ):
        self.provider = provider
        self.model_name = model_name or ""
        self.url = url
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens

        self._backend = None

        # Load checklist config
        if checklist_path is None:
            checklist_path = str(Path(__file__).parent / "rca_checklists.yaml")
        with open(checklist_path) as f:
            self._config = yaml.safe_load(f)

        self._checklist_version = self._config.get("version", "unknown")
        scoring = self._config.get("scoring", {})
        self._threshold = scoring.get("threshold", 0.70)
        self._weights = scoring.get("weights", {})

        # Derive question IDs and counts from config
        self._all_question_ids: list[str] = []
        self._dimension_ids: list[str] = []
        for dim in self._config.get("dimensions", []):
            did = dim["id"]
            self._dimension_ids.append(did)
            if did not in self._weights:
                self._weights[did] = 1.0 / len(self._config["dimensions"])
            for q in dim.get("questions", []):
                self._all_question_ids.append(q["id"])
        self._num_questions = len(self._all_question_ids)
        self._num_dimensions = len(self._dimension_ids)

        # Build system prompt from template
        self._system_prompt = self._SYSTEM_PROMPT_TEMPLATE.replace(
            "{{num_questions}}", str(self._num_questions)
        ).replace("{{num_dimensions}}", str(self._num_dimensions))

    @property
    def backend(self):
        """Lazily initialize the LLM backend only when needed."""
        if self._backend is None:
            try:
                self._backend = get_llm_backend_for_judge(
                    provider=self.provider,
                    model_name=self.model_name or None,
                    api_base=self.url,
                    api_key=self.api_key,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except (SystemExit, Exception) as e:
                print(f"Warning: Failed to initialize LLM backend for judge: {e}")
                print("Returning None - evaluation will be skipped")
                return None
        return self._backend

    # ------------------------------------------------------------------
    # Public interface (drop-in compatible with LLMJudge)
    # ------------------------------------------------------------------

    def judge(
        self,
        solution: str,
        expectation: str,
    ) -> tuple[JudgmentResult, str]:
        """Drop-in replacement for ``LLMJudge.judge()``.

        Parameters
        ----------
        solution : str
            The agent's diagnosis text.
        expectation : str
            Natural-language root-cause description.
        """
        report = self.judge_detailed(solution, expectation)
        return report.verdict, report.reasoning

    def judge_detailed(
        self,
        solution: str,
        expectation: str,
    ):
        """Evaluate solution against expectation using the full checklist.

        Returns a ``JudgmentReport`` with per-dimension scores.
        """
        from sregym.conductor.oracles.llm_as_a_judge.models import (
            DimensionResult,
            JudgmentReport,
            QuestionResult,
        )

        if self.backend is None:
            error_msg = "LLM judge backend is not initialized - skipping evaluation"
            print(f"Warning: {error_msg}")
            return JudgmentReport(
                verdict=None,
                reasoning=error_msg,
                composite_score=0.0,
                checklist_version=self._checklist_version,
                evaluator_model=self._evaluator_model(),
            )

        # Handle empty / "I don't know" answers
        if not solution or not solution.strip():
            return self._empty_report()

        # Build and send prompt
        user_msg = self._build_user_message(solution, expectation)
        raw_results = self._call_llm_with_retry(user_msg)

        # Build question lookup from config
        q_lookup: dict[str, dict] = {}
        for dim in self._config["dimensions"]:
            for q in dim["questions"]:
                q_lookup[q["id"]] = {
                    "text": q["text"],
                    "dim_id": dim["id"],
                    "dim_name": dim["name"],
                }

        # Group answers by dimension
        dim_questions: dict[str, list[QuestionResult]] = {did: [] for did in self._dimension_ids}
        for item in raw_results:
            qid = item["id"]
            info = q_lookup.get(qid, {"text": "", "dim_id": qid[:2], "dim_name": ""})
            qr = QuestionResult(
                question_id=qid,
                question_text=info["text"],
                answer=item["answer"].strip().lower() == "yes",
                evidence=item.get("evidence", ""),
                confidence=item.get("confidence", "Low"),
            )
            dim_questions[info["dim_id"]].append(qr)

        # Score dimensions
        dimensions: list[DimensionResult] = []
        for dim_cfg in self._config["dimensions"]:
            did = dim_cfg["id"]
            qs = dim_questions.get(did, [])
            yes_count = sum(1 for q in qs if q.answer)
            total = len(qs) if qs else 1
            score = yes_count / total
            dimensions.append(
                DimensionResult(
                    dimension_id=did,
                    dimension_name=dim_cfg["name"],
                    score=round(score, 2),
                    questions=qs,
                )
            )

        # Composite score (weighted)
        composite = sum(self._weights.get(d.dimension_id, 1.0 / self._num_dimensions) * d.score for d in dimensions)
        composite = round(composite, 2)

        verdict = JudgmentResult.TRUE if composite >= self._threshold else JudgmentResult.FALSE

        reasoning = self._build_reasoning(verdict, composite, dimensions)

        return JudgmentReport(
            verdict=verdict,
            reasoning=reasoning,
            composite_score=composite,
            dimensions=dimensions,
            checklist_version=self._checklist_version,
            evaluator_model=self._evaluator_model(),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_user_message(
        self,
        solution: str,
        expectation: str,
    ) -> str:
        lines: list[str] = []

        # ---- Section 1: Ground-truth root cause ----
        lines.append("## Ground-Truth Root Cause")
        lines.append(expectation if expectation else "(No fault - system is operating normally)")
        lines.append("")

        # ---- Section 2: Agent diagnosis ----
        lines.append("## Agent Diagnosis")
        lines.append(solution)
        lines.append("")

        # ---- Section 3: Evaluation checklist ----
        lines.append("## Evaluation Checklist")
        for dim in self._config["dimensions"]:
            lines.append(f"\n### {dim['id']} — {dim['name']}")
            lines.append(dim["definition"])
            for q in dim["questions"]:
                hint = q.get("evaluator_hint", "")
                lines.append(f"{q['id']}: {q['text']}")
                if hint:
                    lines.append(f"  Hint: {hint}")
        lines.append("")

        # ---- Section 4: Required output format ----
        lines.append(f"## Required JSON Response (exactly {self._num_questions} objects)")
        lines.append(
            "Apply chain-of-thought reasoning internally for each dimension "
            "before producing the answer.  Output ONLY the JSON array:"
        )
        lines.append("[")
        for i, qid in enumerate(self._all_question_ids):
            comma = "," if i < self._num_questions - 1 else ""
            lines.append(
                f'  {{"id": "{qid}", "answer": "Yes|No", "evidence": "...", "confidence": "High|Medium|Low"}}{comma}'
            )
        lines.append("]")
        return "\n".join(lines)

    def _call_llm_with_retry(self, user_msg: str) -> list[dict]:
        """Call the LLM and parse the response, retrying up to once per failure mode."""
        messages = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=user_msg),
        ]

        last_error: Exception | None = None
        for attempt in range(2):
            response = None
            try:
                response = self.backend.inference(messages)
                response_text = LLMJudge._content_text(response.content)
                results = self._parse_response(response_text, self._all_question_ids)
                return results
            except ChecklistParseError as e:
                last_error = e
                print(f"Checklist parse attempt {attempt + 1} failed: {e}")
                if attempt == 0:
                    if self._was_truncated(response):
                        # The budget, not the model, caused the failure. Repeating
                        # the same request would truncate again.
                        new_budget = self.max_tokens * _TRUNCATION_RETRY_FACTOR
                        print(
                            f"Judge response hit max_tokens={self.max_tokens} "
                            f"(finish_reason=length); retrying with max_tokens={new_budget}"
                        )
                        self.max_tokens = new_budget
                        self.backend.max_tokens = new_budget
                    elif "missing" in str(e).lower() or str(self._num_questions) in str(e):
                        messages = [
                            SystemMessage(content=self._system_prompt),
                            HumanMessage(
                                content=(
                                    user_msg + f"\n\nYour previous response was missing some "
                                    f"questions. Respond with all {self._num_questions}."
                                )
                            ),
                        ]

        # After 2 failures, return defaults for missing questions
        print(f"JudgeParseError: all retries exhausted – {last_error}")
        return [
            {
                "id": qid,
                "answer": "No",
                "evidence": "parse failure — defaulting to No",
                "confidence": "Low",
            }
            for qid in self._all_question_ids
        ]

    @staticmethod
    def _was_truncated(response) -> bool:
        """True when the provider stopped generation because ``max_tokens`` was reached."""
        metadata = getattr(response, "response_metadata", None) or {}
        return str(metadata.get("finish_reason", "")).lower() == "length"

    @staticmethod
    def _parse_response(response_text: str, expected_question_ids: list[str]) -> list[dict]:
        """Return the first complete, valid checklist in the response."""
        expected_ids = set(expected_question_ids)
        last_error = ChecklistParseError("Invalid JSON: no checklist array found")
        for candidate in DiagnosisJudge._json_candidates(response_text):
            try:
                return DiagnosisJudge._validate_checklist(candidate, expected_ids)
            except ChecklistParseError as exc:
                last_error = exc
        raise last_error

    @staticmethod
    def _json_candidates(response_text: str) -> Iterator[object]:
        """Decode outer JSON containers without treating nested values as candidates."""
        decoder = json.JSONDecoder()
        stack: list[str] = []
        start = 0
        # Locate container boundaries only; the JSON decoder checks their syntax.
        # Quoted strings are single tokens, including an unfinished final string.
        for match in re.finditer(r'"(?:\\.|[^"\\])*"?|[\[\]{}]', response_text):
            token = match.group()
            if token.startswith('"'):
                continue
            if token in ("[", "{"):
                if not stack:
                    start = match.start()
                stack.append("]" if token == "[" else "}")
                continue
            if not stack:
                continue
            if token != stack.pop():
                # A broken outer container does not define safe candidate boundaries.
                return
            if not stack:
                try:
                    candidate, end = decoder.raw_decode(response_text, start)
                except (ValueError, RecursionError):
                    # ValueError also covers Python's integer conversion limit.
                    continue
                if end == match.end():
                    yield candidate

    @staticmethod
    def _validate_checklist(candidate: object, expected_ids: set[str]) -> list[dict]:
        """Validate the fields consumed by grading before accepting a candidate."""
        if not isinstance(candidate, list):
            raise ChecklistParseError("Response is not a JSON array")

        received_ids: set[str] = set()
        for item in candidate:
            if not isinstance(item, dict):
                raise ChecklistParseError("Each checklist item must be an object")
            qid = item.get("id")
            if not isinstance(qid, str) or qid not in expected_ids:
                raise ChecklistParseError(f"Invalid question ID: {qid!r}")
            if qid in received_ids:
                raise ChecklistParseError(f"Duplicate question ID: {qid}")
            answer = item.get("answer")
            if not isinstance(answer, str) or answer.strip().lower() not in ("yes", "no"):
                raise ChecklistParseError(f"Question {qid} must have a Yes/No answer")
            for field in ("evidence", "confidence"):
                if field in item and not isinstance(item[field], str):
                    raise ChecklistParseError(f"Question {qid} must have a string {field}")
            received_ids.add(qid)

        if missing := expected_ids - received_ids:
            raise ChecklistParseError(
                f"Expected {len(expected_ids)} questions, got {len(candidate)}. Missing: {sorted(missing)}"
            )
        return candidate

    def _empty_report(self):
        """Return a report where all questions score No (empty/unknown answer)."""
        from sregym.conductor.oracles.llm_as_a_judge.models import (
            DimensionResult,
            JudgmentReport,
            QuestionResult,
        )

        dimensions = []
        for dim in self._config["dimensions"]:
            qs = [
                QuestionResult(
                    question_id=q["id"],
                    question_text=q["text"],
                    answer=False,
                    evidence="empty answer",
                    confidence="Low",
                )
                for q in dim["questions"]
            ]
            dimensions.append(
                DimensionResult(
                    dimension_id=dim["id"],
                    dimension_name=dim["name"],
                    score=0.0,
                    questions=qs,
                )
            )

        return JudgmentReport(
            verdict=JudgmentResult.FALSE,
            reasoning=self._build_reasoning(JudgmentResult.FALSE, 0.0, dimensions),
            composite_score=0.0,
            dimensions=dimensions,
            checklist_version=self._checklist_version,
            evaluator_model=self._evaluator_model(),
        )

    def _evaluator_model(self) -> str:
        """Return the model selected by the initialized backend."""
        backend_model = getattr(self._backend, "model_name", None)
        return backend_model or self.model_name or os.environ.get("JUDGE_MODEL_ID", "")

    @staticmethod
    def _build_reasoning(
        verdict: JudgmentResult,
        composite: float,
        dimensions: list,
    ) -> str:
        parts = [f"Verdict: {verdict.value} (composite={composite:.2f})."]
        dim_strs = []
        weakest = None
        weakest_score = float("inf")
        checklist_items: list[dict[str, str]] = []
        for d in dimensions:
            dim_strs.append(f"{d.dimension_id} {d.dimension_name}: {d.score:.2f}")
            if d.score < weakest_score:
                weakest_score = d.score
                weakest = d
            for q in d.questions:
                checklist_items.append(
                    {
                        "id": q.question_id,
                        "answer": "Yes" if q.answer else "No",
                        "evidence": q.evidence or "",
                        "confidence": q.confidence or "Low",
                    }
                )
        parts.append(" | ".join(dim_strs) + ".")
        if weakest:
            parts.append(f"Weakest dimension: {weakest.dimension_name} ({weakest.score:.2f}).")
        parts.append("Checklist details: " + json.dumps(checklist_items, ensure_ascii=True))
        return " ".join(parts)


# ===================================================================
# CLI test harness (unchanged)
# ===================================================================


def load_test_data(yaml_path: str) -> list[dict]:
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    return data


def main():
    script_dir = Path(__file__).parent
    data_path = script_dir / "data.yaml"

    if not data_path.exists():
        print(f"Test data file not found: {data_path}")
        return

    test_cases = load_test_data(str(data_path))
    print(f"Loaded {len(test_cases)} test cases from {data_path}")

    judge = DiagnosisJudge()

    total_cases = len(test_cases)
    correct = 0
    incorrect = 0
    results = []

    for i, test_case in enumerate(test_cases, 1):
        description = test_case.get("description", "")
        answer = test_case.get("answer", "")
        expected_judgment = test_case.get("oracle", "")

        print(f"\n{'=' * 80}")
        print(f"Test Case {i}/{total_cases}")
        print(
            f"Expected Root Cause: {description[:100]}..."
            if len(description) > 100
            else f"Expected Root Cause: {description}"
        )
        print(f"Agent Answer: {answer[:100]}..." if len(answer) > 100 else f"Agent Answer: {answer}")
        print(f"Expected Judgment: {expected_judgment}")

        try:
            actual_judgment, reasoning = judge.judge(solution=answer, expectation=description)

            expected_normalized = expected_judgment.strip().lower().replace(" ", "")
            actual_normalized = actual_judgment.value.lower().replace(" ", "")
            is_correct = expected_normalized == actual_normalized

            if is_correct:
                correct += 1
                status = "✅ CORRECT"
            else:
                incorrect += 1
                status = "❌ INCORRECT"

            print(f"Actual Judgment: {actual_judgment.value}")
            print(f"Status: {status}")

            results.append(
                {
                    "test_case": i,
                    "expected": expected_judgment,
                    "actual": actual_judgment.value,
                    "correct": is_correct,
                    "reasoning": reasoning,
                }
            )

        except Exception as e:
            print(f"Error processing test case {i}: {e}")
            incorrect += 1
            results.append(
                {
                    "test_case": i,
                    "expected": expected_judgment,
                    "actual": f"ERROR: {str(e)}",
                    "correct": False,
                }
            )

    print(f"\n{'=' * 80}")
    print("SUMMARY")
    print(f"{'=' * 80}")
    print(f"Total test cases: {total_cases}")
    print(f"Correct: {correct} ({correct / total_cases * 100:.1f}%)")
    print(f"Incorrect: {incorrect} ({incorrect / total_cases * 100:.1f}%)")
    print("\nDetailed Results:")
    for result in results:
        status_symbol = "✅" if result["correct"] else "❌"
        print(f"  {status_symbol} Case {result['test_case']}: Expected={result['expected']}, Actual={result['actual']}")


if __name__ == "__main__":
    main()
