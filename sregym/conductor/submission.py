"""Shared submission-stage contract for the Conductor and its APIs."""

from typing import Literal

type SubmissionStage = Literal["diagnosis", "mitigation"]
SUBMISSION_STAGE_ORDER = {"diagnosis": 0, "mitigation": 1}
SUBMISSION_STAGES = frozenset(SUBMISSION_STAGE_ORDER)


class EvaluationInProgress(RuntimeError):
    """A submission for the current stage was already accepted."""

    def __init__(self, stage: str | None):
        self.stage = stage
        super().__init__(f"A submission for stage {stage!r} is already being evaluated.")


class SubmissionStageMismatch(RuntimeError):
    """A submission was sent for a stage that is not currently active."""

    def __init__(self, expected_stage: str, current_stage: str | None):
        self.expected_stage = expected_stage
        self.current_stage = current_stage
        super().__init__(f"Submission targets stage {expected_stage!r}, but the current stage is {current_stage!r}.")


class SubmissionAttemptClosed(RuntimeError):
    """The attempt no longer accepts new submission requests."""


class SubmissionAttemptMismatch(RuntimeError):
    """A request registered for an older attempt reached a newer attempt."""

    def __init__(self, expected_generation: int, current_generation: int):
        self.expected_generation = expected_generation
        self.current_generation = current_generation
        super().__init__(
            f"Submission belongs to attempt generation {expected_generation}, "
            f"but the current generation is {current_generation}."
        )
