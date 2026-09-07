"""Base class for evaluation oracles."""

from abc import ABC, abstractmethod


class Oracle(ABC):
    # Long-running oracles can request a larger grading budget. None preserves
    # the runner's default; this does not extend agent or cleanup deadlines.
    evaluation_timeout_seconds: float | None = None

    def __init__(self, problem):
        self.problem = problem

    def capture_baseline(self) -> None:
        """Record the healthy pre-fault cluster state.

        Called once the app is deployed and before the fault is injected.
        Oracles cannot do this in __init__: the Problem is constructed before
        deploy_app(), when the namespace does not exist yet. Defaults to a
        no-op for oracles that need no baseline.
        """
        return

    @abstractmethod
    def evaluate(self, solution, trace, duration) -> dict:
        """Evaluate a solution."""
        pass
