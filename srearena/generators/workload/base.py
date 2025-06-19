from abc import ABC, abstractmethod

from pydantic.dataclasses import dataclass

# Two types of workload generators:
# 1. Constantly running workload generator
# 2. Workload generator that runs for a fixed duration
# By repeating type 2, we can always assume it's constantly running.

# Two purposes:
# 1. To generate traces
# 2. Validation


@dataclass
class WorkloadEntry:
    time: float  # Start time of the workload run
    number: int  # Number of requests generated in this workload run
    log: str  # Log of the workload run
    ok: bool  # Indicates if the workload was successful


class WorkloadManager(ABC):
    """
    Constantly running workload generator.
    """

    def __init__(self):
        super().__init__()

    @abstractmethod
    def start(self, *args, **kwargs):
        """
        Start the workload generator.
        """
        pass

    @abstractmethod
    def stop(self, *args, **kwargs):
        """
        Stop the workload generator.
        """
        pass

    @abstractmethod
    def collect(self, number=100, since_seconds=None) -> list[WorkloadEntry]:
        """
        Run the workload generator until collected data is sufficient.
        - Number of requests should be at least `number` starting from `since_seconds` ago.
        - If `since_seconds` is not provided, it should start from the current time.
        - `since_seconds` is a relative time in seconds, not an absolute timestamp.
        """
        pass

    @abstractmethod
    def recent_entries(self, duration=30) -> list[WorkloadEntry]:
        """
        Return recently collected data within the given duration (seconds).
        """
        pass
