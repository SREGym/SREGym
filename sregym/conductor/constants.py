from enum import StrEnum

from sregym.cleanup import cleanup_drain_timeout as cleanup_drain_timeout


class StartProblemResult(StrEnum):
    SUCCESS = "success"
