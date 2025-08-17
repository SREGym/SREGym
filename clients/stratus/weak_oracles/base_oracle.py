from abc import ABC, abstractmethod
from typing import List


class OracleResult:
    success: bool
    issues: List[str]

    def __init__(self, success: bool, issues: List[str]):
        self.success = success
        self.issues = issues

    def __str__(self):
        return f"Your last mitigation attempt [{"has succeeded" if self.success else "has failed"}]. The potential issues are [{"no issues as you have succeeded" if self.success else self.issues}]"


class BaseOracle(ABC):
    @abstractmethod
    async def validate(self, **kwargs) -> OracleResult:
        pass
