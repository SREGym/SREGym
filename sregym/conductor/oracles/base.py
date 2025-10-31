"""Base class for evaluation oracles."""

from abc import ABC, abstractmethod


class Oracle(ABC):
    def __init__(self, problem):
        self.problem = problem

    @abstractmethod
    def evaluate(self, solution, trace, duration) -> dict:
        """Evaluate a solution."""
        pass
