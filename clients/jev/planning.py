"""Compare agent-proposed diagnostic tests without executing them."""

from pydantic import BaseModel, ConfigDict, Field


class DiagnosticTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str = Field(min_length=1, max_length=600)
    command: str = Field(min_length=1, max_length=1200)
    supports_if: str = Field(min_length=1, max_length=400)
    rejects_if: str = Field(min_length=1, max_length=400)


def planning_questions(tests: list[DiagnosticTest]) -> dict:
    questions = {
        "next_test": {
            "type": "choice",
            "instructions": "Which proposed read-only test best distinguishes competing causes of the current application failure? Prefer a fresh, interpretable observation of failing behavior over rereading historical warnings or confirming healthy unrelated components. Select a test, not the most persuasive written diagnosis. A repair is not a diagnostic test.",
            "criteria": {
                **{f"test_{i}": f"Run candidate test {i}: {test.hypothesis}" for i, test in enumerate(tests, 1)},
                "revise_tests": "None offers a safe, interpretable check of the active failure; propose different tests.",
            },
        }
    }
    for i in range(1, len(tests) + 1):
        questions[f"value_{i}"] = {
            "type": "score",
            "instructions": f"Rate the diagnostic value of candidate test {i}, including whether its two predicted outcomes distinguish competing explanations. Judge a read-only observation, not a repair or submission.",
            "criteria": [
                "Changes resources, cannot distinguish causes, or does not examine the current problem.",
                "Checks a historical or weakly related symptom without testing a causal explanation.",
                "Adds relevant evidence but several explanations predict the same result.",
                "Tests an active failure and can meaningfully reject an alternative explanation.",
                "Safely isolates the failing mechanism with clear contrasting outcomes.",
            ],
        }
    return questions


def planning_guidance(result: dict, tests: list[DiagnosticTest]) -> dict:
    if "error" in result:
        return {"next_step": "Planning advice unavailable. Continue with your own safe diagnostic checks."}
    answer = result["answers"]["next_test"]
    if answer["choice"] == "revise_tests":
        return {"next_step": "Propose different read-only tests of the actual failing behavior.", "tests": []}
    ranked = sorted(
        ((key, probability) for key, probability in answer["probabilities"].items() if key != "revise_tests"),
        key=lambda item: item[1],
        reverse=True,
    )
    # A diffuse ranking is a reason to collect more evidence, not choose one
    # explanation with unjustified certainty. The agent still checks safety.
    count = 2 if answer["confidence"] < 0.5 else 1
    return {
        "next_step": "Run these diagnostic checks separately, inspect their outputs, and compare the predicted outcomes before changing resources. These are test priorities, not proven diagnoses.",
        "tests": [
            {"id": key, "probability": probability, **tests[int(key.removeprefix("test_")) - 1].model_dump()}
            for key, probability in ranked[:count]
        ],
    }
