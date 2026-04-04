from abc import ABC, abstractmethod


class OracleResult:
    success: bool
    issues: list[str]

    def __init__(self, success: bool, issues: list[str]):
        self.success = success
        self.issues = issues

    def __repr__(self):
        return f"Your last mitigation attempt [{'has succeeded' if self.success else 'has failed'}]. The potential issues are [{'no issues as you have succeeded' if self.success else self.issues}]"

    def __str__(self):
        return self.__repr__()


class BaseOracle(ABC):
    @abstractmethod
    def validate(self, **kwargs) -> OracleResult:
        pass

    def __repr__(self):
        return f"{self.__class__.__name__}"
