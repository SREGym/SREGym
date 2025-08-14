from abc import ABC, abstractmethod
from typing import List


class OracleResult:
    success: bool
    issues: List[str]

    def __init__(self, success: bool, issues: List[str]):
        self.success = success
        self.issues = issues


class BaseOracle(ABC):
    @abstractmethod
    def validate(self, **kwargs) -> OracleResult:
        pass
