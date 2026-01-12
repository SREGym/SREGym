"""LLM-as-a-Judge Oracle for evaluating agent solutions against expected root causes."""

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage

from llm_backend.init_backend import get_llm_backend_for_tools

load_dotenv()


class JudgmentResult(str, Enum):
    TRUE = "True"  # Correct diagnosis - agent identified the root cause
    FALSE = "False"  # Incorrect diagnosis - agent did not identify the root cause


class LLMJudge:
    def __init__(
        self,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
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
            self._backend = get_llm_backend_for_tools()
        return self._backend

    def judge(self, solution: str, expectation: str) -> tuple[JudgmentResult, str]:
        """
        Judge whether a solution matches the expectation.

        Returns:
            tuple[JudgmentResult, str]: A tuple of (judgment, reasoning)
        """
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


def load_test_data(yaml_path: str) -> list[dict]:
    with open(yaml_path, "r") as f:
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
    judge = LLMJudge()

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

        print(f"\n{'='*80}")
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
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"Total test cases: {total_cases}")
    print(f"Correct: {correct} ({correct/total_cases*100:.1f}%)")
    print(f"Incorrect: {incorrect} ({incorrect/total_cases*100:.1f}%)")
    print(f"\nDetailed Results:")

    for result in results:
        status_symbol = "✅" if result["correct"] else "❌"
        print(f"  {status_symbol} Case {result['test_case']}: Expected={result['expected']}, Actual={result['actual']}")


if __name__ == "__main__":
    main()
