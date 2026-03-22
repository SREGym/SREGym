"""LLM-as-a-Judge Oracle for evaluating agent solutions against expected root causes."""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from llm_backend.get_llm_backend import LiteLLMBackend
from llm_backend.init_backend import get_llm_backend_for_judge


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
        # If any custom settings are provided in using RCAJudge for evaluation stand-alone, use them to create a LiteLLMBackend directly.
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


class ChecklistParseError(Exception):
    """Raised when the LLM response cannot be parsed into checklist results."""


class JudgeParseError(Exception):
    """Raised after all retries are exhausted when parsing checklist results."""


class JudgmentResult(str, Enum):
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
        max_tokens: int = 4096,
    ):
        # Store parameters for lazy initialization
        self.provider = provider
        self.model_name = model_name
        self.url = url
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens

        # Backend will be initialized lazily on first use
        self._backend = None

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
                # Catch both SystemExit (from exit(1) calls) and other exceptions
                print(f"Warning: Failed to initialize LLM backend for judge: {e}")
                print("Returning None - evaluation will be skipped")
                return None
        return self._backend

    def judge(self, solution: str, expectation: str) -> tuple[JudgmentResult, str]:
        """
        Judge whether a solution matches the expectation.

        Returns:
            tuple[JudgmentResult, str]: A tuple of (judgment, reasoning)
            Returns (None, error_message) if backend is not initialized
        """
        # Check if backend is initialized
        if self.backend is None:
            error_msg = "LLM judge backend is not initialized - skipping evaluation"
            print(f"Warning: {error_msg}")
            return None, error_msg

        system_prompt = """You are an expert judge evaluating whether an agent's diagnosis of a system issue matches the expected root cause.

Your task is to compare the agent's answer with the expected root cause and determine if they are semantically equivalent.

Classification criteria:
- **True**: The agent correctly identified the root cause. The diagnosis captures the essential problem even if worded differently.
- **False**: The agent did not identify the root cause. This includes cases where the agent identified a different problem, misdiagnosed the root cause, or failed to identify any problem when one exists.

You must respond with EXACTLY ONE of these two values: True or False

Your response should be in the following JSON format:
{
    "judgment": "True|False",
    "reasoning": "Brief explanation of why you made this judgment"
}"""

        user_prompt = f"""Expected Root Cause:
{expectation if expectation else "(No fault - system is operating normally)"}

Agent's Answer:
{solution}

Evaluate whether the agent's answer correctly identifies the root cause. Respond in JSON format with your judgment and reasoning."""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

        try:
            # Get response from LLM
            response = self.backend.inference(messages)
            response_text = response.content.strip()

            print(f"LLM Response: {response_text}")

            # Parse the response
            judgment, reasoning = self._parse_judgment(response_text)
            print(f"Parsed judgment: {judgment}")

            return judgment, reasoning

        except Exception as e:
            print(f"Error during judgment: {e}")
            raise

    def _parse_judgment(self, response_text: str) -> tuple[JudgmentResult, str]:
        """
        Parse the judgment response from the LLM.

        Returns:
            tuple[JudgmentResult, str]: A tuple of (judgment, reasoning)
        """
        reasoning = ""
        try:
            # Remove markdown code blocks if present
            clean_text = re.sub(r"```json\s*|\s*```", "", response_text)
            clean_text = clean_text.strip()

            response_json = json.loads(clean_text)
            judgment_str = response_json.get("judgment", "").strip()
            reasoning = response_json.get("reasoning", "")

            print(f"Reasoning: {reasoning}")

        except json.JSONDecodeError:
            # Fallback: try to extract judgment directly from text
            print("Failed to parse JSON, attempting direct extraction")
            judgment_str = response_text
            reasoning = "Failed to parse structured response"

        # Normalize the judgment string
        judgment_str = judgment_str.strip().lower()

        # Map to JudgmentResult
        if judgment_str == "true":
            return JudgmentResult.TRUE, reasoning
        elif judgment_str == "false":
            return JudgmentResult.FALSE, reasoning
        else:
            raise ValueError(f"Could not parse judgment from response: {response_text}")


class DiagnosisJudge:
    """Checklist-based RCA evaluator that scores 5 dimensions in one LLM call."""

    _SYSTEM_PROMPT_TEMPLATE = (
        "You are an expert SRE evaluator assessing an AI agent's root cause analysis.\n"
        "You will be given:\n"
        "  1. A ground-truth fault specification (what actually happened)\n"
        "  2. The agent's diagnosis (what the agent claims happened)\n"
        "  3. A checklist of {{num_questions}} Yes/No questions grouped across {{num_dimensions}} evaluation dimensions\n"
        "\n"
        "For EACH of the {{num_questions}} questions respond with:\n"
        '  - id: the question ID exactly as given (e.g. "D1-Q1")\n'
        '  - answer: exactly "Yes" or "No"\n'
        "  - evidence: quote or paraphrase ≤30 words from the diagnosis supporting your answer\n"
        '  - confidence: "High", "Medium", or "Low"\n'
        "\n"
        "Rules:\n"
        "  - Answer based ONLY on what is explicitly stated in the diagnosis\n"
        "  - Do NOT infer information not present in the text\n"
        '  - "Yes" always means the positive/correct outcome is present\n'
        "  - Treat each question independently\n"
        "  - Respond ONLY with the JSON array, no preamble, no commentary"
    )

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
                # Equal weight fallback
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
                print("Returning None - evaluation will be skipped")
                return None
        return self._backend

    # -- public interface (drop-in compatible with LLMJudge) --

    def judge(self, solution: str, expectation: str) -> tuple[JudgmentResult, str]:
        """Drop-in replacement for LLMJudge.judge()."""

        report = self.judge_detailed(solution, expectation)
        return report.verdict, report.reasoning

    def judge_detailed(self, solution: str, expectation: str):
        """Evaluate solution against expectation using the full checklist.

        Returns a JudgmentReport with per-dimension scores.
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
        user_msg = self._build_user_message(solution, expectation)
        raw_results = self._call_llm_with_retry(user_msg)

        # Build question lookup from config
        q_lookup: dict[str, dict] = {}
        for dim in self._config["dimensions"]:
            for q in dim["questions"]:
                q_lookup[q["id"]] = {"text": q["text"], "dim_id": dim["id"], "dim_name": dim["name"]}

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
            score = yes_count / 3.0 if qs else 0.0
            dimensions.append(
                DimensionResult(
                    dimension_id=did,
                    dimension_name=dim_cfg["name"],
                    score=round(score, 2),
                    questions=qs,
                )
            )

        # Composite score (weighted)
        composite = sum(self._weights.get(d.dimension_id, 0.20) * d.score for d in dimensions)
        composite = round(composite, 2)

        verdict = JudgmentResult.TRUE if composite >= self._threshold else JudgmentResult.FALSE

        # Build legacy reasoning string
        reasoning = self._build_reasoning(verdict, composite, dimensions)

        return JudgmentReport(
            verdict=verdict,
            reasoning=reasoning,
            composite_score=composite,
            dimensions=dimensions,
            checklist_version=self._checklist_version,
            evaluator_model=self.model_name,
        )

    # -- internal helpers --

    def _build_user_message(self, solution: str, expectation: str) -> str:
        lines = [
            "## Ground-Truth Fault Specification",
            expectation if expectation else "(No fault - system is operating normally)",
            "",
            "## Agent Diagnosis",
            solution,
            "",
            "## Evaluation Checklist",
        ]
        for dim in self._config["dimensions"]:
            lines.append(f"\n### {dim['id']} — {dim['name']}")
            lines.append(dim["definition"])
            for q in dim["questions"]:
                lines.append(f"{q['id']}: {q['text']}")
        lines.append("")
        lines.append(f"## Required JSON Response (exactly {self._num_questions} objects)")
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
                        # Missing questions – append hint
                        messages = [
                            SystemMessage(content=self._system_prompt),
                            HumanMessage(
                                content=user_msg + f"\n\nYour previous response was missing some questions. "
                                f"Respond with all {self._num_questions}."
                            ),
                        ]
                    # else: invalid JSON – retry with identical prompt

        # After 2 failures, return defaults for missing questions
        print(f"JudgeParseError: all retries exhausted – {last_error}")
        return [
            {"id": qid, "answer": "No", "evidence": "missing", "confidence": "Low"} for qid in self._all_question_ids
        ]

    @staticmethod
    def _parse_response(response_text: str, expected_question_ids: list[str]) -> list[dict]:
        """Parse the LLM JSON response into a list of question result dicts."""
        # Strip markdown fences
        clean = re.sub(r"```(?:json)?\s*|\s*```", "", response_text).strip()
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
            reasoning="Verdict: False (composite=0.00). Empty or missing diagnosis.",
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
        for d in dimensions:
            dim_strs.append(f"{d.dimension_id} {d.dimension_name}: {d.score:.2f}")
            if d.score < weakest_score:
                weakest_score = d.score
                weakest = d
        parts.append(" | ".join(dim_strs) + ".")
        if weakest:
            parts.append(f"Weakest dimension: {weakest.dimension_name} ({weakest.score:.2f}).")
        return " ".join(parts)


def load_test_data(yaml_path: str) -> list[dict]:
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    return data


def main():
    # Get the directory of this script
    script_dir = Path(__file__).parent
    data_path = script_dir / "data.yaml"

    if not data_path.exists():
        print(f"Test data file not found: {data_path}")
        return

    # Load test data
    test_cases = load_test_data(str(data_path))
    print(f"Loaded {len(test_cases)} test cases from {data_path}")

    # Initialize judge
    # judge = LLMJudge()
    judge = DiagnosisJudge()

    # Track results
    total_cases = len(test_cases)
    correct = 0
    incorrect = 0
    results = []

    # Evaluate each test case
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
            # Get judgment from LLM
            actual_judgment, reasoning = judge.judge(solution=answer, expectation=description)

            # Normalize expected judgment for comparison
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

    # Print summary
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
