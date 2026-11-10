"""LLM judge whose mechanism questions are required, not just weighted.

The stock checklist passes a diagnosis at a composite score of 0.70, so a
diagnosis with the right component and symptom but the wrong mechanism can
pass on localization and scope alone. Dependency-Track problems whose fix can be
reached without understanding the defect (a blind re-analysis, a blind
watermark rewind) gate on the mechanism instead: besides the composite
threshold, the judge must answer Yes to every question in ``REQUIRED``
(D2-Q1, "same mechanism as the ground truth", and D2-Q3, "no incorrect fault
type"). Each problem's ``root_cause`` ends with "Grading requirements" that
tell the judge what the core mechanism is, which detail only adds credit
(D2-Q2), and which plausible wrong mechanisms must answer No.
"""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle


class MechanismGatedJudgeOracle(LLMAsAJudgeOracle):
    REQUIRED = ("D2-Q1", "D2-Q3")

    def evaluate(self, solution, duration=None) -> dict:
        result = super().evaluate(solution, duration)
        if result.get("success") is None:
            return result
        answers = {item["id"]: item["answer"] for item in result.get("checklist", [])}
        missing = [qid for qid in self.REQUIRED if answers.get(qid) != "Yes"]
        result["required_items"] = {qid: answers.get(qid) for qid in self.REQUIRED}
        if missing and result.get("success"):
            result["success"] = False
            result["judgment"] = "False"
            result["gated_by"] = missing
            print(f"❌ Required mechanism item(s) not satisfied: {', '.join(missing)}")
        return result
