"""Data classes for the RCAJudge evaluation system."""

from dataclasses import dataclass, field

from sregym.conductor.oracles.llm_as_a_judge.judge import JudgmentResult


@dataclass
class QuestionResult:
    """Result of a single checklist question evaluation."""

    question_id: str  # "D1-Q1"
    question_text: str
    answer: bool  # True = Yes
    evidence: str  # ≤30-word quote/paraphrase from diagnosis
    confidence: str  # "High" | "Medium" | "Low"


@dataclass
class DimensionResult:
    """Result of a single evaluation dimension (3 questions)."""

    dimension_id: str  # "D1"..."D5"
    dimension_name: str
    score: float
    questions: list[QuestionResult] = field(default_factory=list)


@dataclass
class JudgmentReport:
    """Complete judgment report with legacy-compatible fields and new structured data."""

    # Legacy-compatible
    verdict: JudgmentResult
    reasoning: str
    # New
    composite_score: float
    dimensions: list[DimensionResult] = field(default_factory=list)
    checklist_version: str = ""
    evaluator_model: str = ""
