"""LLM-as-a-Judge Oracle for evaluating agent solutions against expected root causes.

v3.0 improvements over v2.0
---------------------------
1. **Fault-spec-aware ground truth** – The judge now receives a structured
   ``FaultSpec`` (fault category, mechanism, target component, injection
    parameters) extracted directly from the Problem's
   fault-injection logic, instead of relying solely on a short natural-language
   ``root_cause`` string.
2. **Chain-of-thought + in-context learning** – The system prompt now instructs
   the judge to reason step-by-step *per dimension* before answering, and
   includes a worked example for every dimension so the judge sees the expected
   reasoning depth and format.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from llm_backend.get_llm_backend import LiteLLMBackend
from llm_backend.init_backend import get_llm_backend_for_judge

# ---------------------------------------------------------------------------
# Backend helpers
# ---------------------------------------------------------------------------


def _create_judge_backend(
    provider: str | None,
    model_name: str | None,
    url: str | None,
    api_key: str | None,
    temperature: float,
    max_tokens: int,
) -> LiteLLMBackend:
    """Use SREGym llm_backend directly for judge backend initialization."""
    if provider or model_name or url or api_key:
        if not model_name:
            raise ValueError("model_name is required when overriding judge backend settings")

        return LiteLLMBackend(
            provider=provider or "openai",
            model_name=model_name,
            url=url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    return get_llm_backend_for_judge()


# ---------------------------------------------------------------------------
# Exceptions and enums
# ---------------------------------------------------------------------------


class ChecklistParseError(Exception):
    """Raised when the LLM response cannot be parsed into checklist results."""


class JudgeParseError(Exception):
    """Raised after all retries are exhausted when parsing checklist results."""


class JudgmentResult(str, Enum):
    TRUE = "True"  # Correct diagnosis
    FALSE = "False"  # Incorrect diagnosis


# ===================================================================
# LLMJudge  (simple binary judge – kept for backward compatibility)
# ===================================================================


class LLMJudge:
    def __init__(
        self,
        provider: str | None = None,
        model_name: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
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
        if self._backend is None:
            try:
                self._backend = _create_judge_backend(
                    provider=self.provider,
                    model_name=self.model_name,
                    url=self.url,
                    api_key=self.api_key,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except (SystemExit, Exception) as e:
                print(f"Warning: Failed to initialize LLM backend for judge: {e}")
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
            return self._parse_judgment(response.content.strip())
        except Exception as e:
            print(f"Error during judgment: {e}")
            raise

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

    Key improvements over v2.0:
    - Accepts an optional ``problem`` reference to extract a rich FaultSpec.
    - Uses chain-of-thought reasoning with in-context examples per dimension.
    """

    # ------------------------------------------------------------------
    # System prompt: chain-of-thought + in-context examples
    # ------------------------------------------------------------------
    _SYSTEM_PROMPT_TEMPLATE = """\
You are an expert SRE evaluator assessing an AI agent's root cause analysis.

You will be given:
  1. A **structured fault specification** extracted from the fault-injection
     framework (what actually happened — including fault category, mechanism,
      target component, and injection parameters).
  2. The **ground-truth root cause** in natural language.
  3. The agent's **diagnosis** (what the agent claims happened).
  4. A checklist of {{num_questions}} Yes/No questions grouped across
     {{num_dimensions}} evaluation dimensions.

## How to evaluate (chain-of-thought)

For EACH dimension, follow these steps **in order**:

  **Step 1 — Understand the dimension.**  Re-read the dimension definition and
  its evaluator hints.

    **Step 2 — Gather evidence.**  Scan the agent's *diagnosis text* for
    claims relevant to each dimension.

  **Step 3 — Compare against ground truth.**  Use the *structured fault spec*
  (not just the natural-language root cause) as the authoritative reference.
  Pay special attention to:
    - `target_component` and `target_resource_kind` for localization (D1)
    - `fault_category`, `fault_mechanism`, and `parameters` for characterization (D2)
        - Fault impact and affected components for scope precision (D3)

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

Ground-truth spec says: `target_component=checkout`, `target_resource_kind=Deployment`.
Agent diagnosis says: "The checkout deployment has a misconfigured port."

Reasoning (internal): The agent names "checkout deployment" which matches the
ground-truth target_component ("checkout") and resource kind ("Deployment").
The agent does not blame any other component.  → D1-Q1: Yes, D1-Q2: Yes, D1-Q3: Yes.

### Example — D2 Fault Characterization

Ground-truth spec says: `fault_category=misconfiguration`,
`fault_mechanism=incorrect_port_assignment`, `parameters={env_var: PRODUCT_CATALOG_ADDR, incorrect_port: 8082, correct_port: 8080}`.
Agent diagnosis says: "checkout can't reach product-catalog because the
PRODUCT_CATALOG_ADDR env var points to port 8082 instead of 8080."

Reasoning: Category is misconfiguration ✓.  Mechanism is incorrect port ✓.
Specific detail (env var name, wrong port value) matches parameters ✓.
→ D2-Q1: Yes, D2-Q2: Yes, D2-Q3: Yes.

### Example — D3 Scope Precision

Ground-truth spec says: `target_component=frontend`, `fault_category=misconfiguration`.
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
        max_tokens: int = 4096,
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
                self._backend = _create_judge_backend(
                    provider=self.provider,
                    model_name=self.model_name,
                    url=self.url,
                    api_key=self.api_key,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except (SystemExit, Exception) as e:
                print(f"Warning: Failed to initialize LLM backend for judge: {e}")
                return None
        return self._backend

    # ------------------------------------------------------------------
    # Public interface (drop-in compatible with LLMJudge)
    # ------------------------------------------------------------------

    def judge(
        self,
        solution: str,
        expectation: str,
        *,
        problem=None,
    ) -> tuple[JudgmentResult, str]:
        """Drop-in replacement for ``LLMJudge.judge()``.

        Parameters
        ----------
        solution : str
            The agent's diagnosis text.
        expectation : str
            Natural-language root-cause description.
        problem : Problem, optional
            If provided, a structured FaultSpec is extracted and included in the
            judge prompt.
        """
        report = self.judge_detailed(solution, expectation, problem=problem)
        return report.verdict, report.reasoning

    def judge_detailed(
        self,
        solution: str,
        expectation: str,
        *,
        problem=None,
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
                evaluator_model=self.model_name,
            )

        # Handle empty / "I don't know" answers
        if not solution or not solution.strip():
            return self._empty_report()

        # Build and send prompt
        user_msg = self._build_user_message(solution, expectation, problem=problem)
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
            evaluator_model=self.model_name,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_fault_spec_section(self, problem) -> str:
        """Extract a structured fault spec from the Problem object."""
        try:
            from sregym.conductor.oracles.llm_as_a_judge.fault_spec_extractor import (
                extract_fault_spec,
            )

            spec = extract_fault_spec(problem)
            return spec.to_prompt_section()
        except Exception as e:
            print(f"Warning: Could not extract fault spec from problem: {e}")
            return ""

    def _build_user_message(
        self,
        solution: str,
        expectation: str,
        *,
        problem=None,
    ) -> str:
        lines: list[str] = []

        # ---- Section 1: Ground-truth fault specification ----
        lines.append("## Ground-Truth Fault Specification")
        lines.append("")

        # If a Problem object is available, extract a structured spec
        if problem is not None:
            fault_spec_text = self._build_fault_spec_section(problem)
            if fault_spec_text:
                lines.append(fault_spec_text)
                lines.append("")

        # Always include the natural-language root cause as a fallback
        lines.append("### Natural-Language Root Cause")
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
            try:
                response = self.backend.inference(messages)
                response_text = response.content.strip()
                results = self._parse_response(response_text, self._all_question_ids)
                return results
            except ChecklistParseError as e:
                last_error = e
                print(f"Checklist parse attempt {attempt + 1} failed: {e}")
                if attempt == 0:
                    if "missing" in str(e).lower() or str(self._num_questions) in str(e):
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
    def _parse_response(response_text: str, expected_question_ids: list[str]) -> list[dict]:
        """Parse the LLM JSON response into a list of question result dicts."""
        # Strip markdown fences
        clean = re.sub(r"```(?:json)?\s*|\s*```", "", response_text).strip()

        # Sometimes the model emits chain-of-thought text before the JSON array.
        # Try to find the outermost JSON array.
        bracket_start = clean.find("[")
        bracket_end = clean.rfind("]")
        if bracket_start != -1 and bracket_end != -1 and bracket_end > bracket_start:
            clean = clean[bracket_start : bracket_end + 1]

        try:
            data = json.loads(clean)
        except json.JSONDecodeError as exc:
            raise ChecklistParseError(f"Invalid JSON: {exc}") from exc

        if not isinstance(data, list):
            raise ChecklistParseError("Response is not a JSON array")

        expected_ids = set(expected_question_ids)
        num_expected = len(expected_ids)
        received_ids = {item.get("id") for item in data if isinstance(item, dict)}

        if len(data) < num_expected or not expected_ids.issubset(received_ids):
            missing = expected_ids - received_ids
            raise ChecklistParseError(f"Expected {num_expected} questions, got {len(data)}. Missing: {missing}")

        return data

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
            evaluator_model=self.model_name,
        )

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
