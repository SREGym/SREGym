import time
from abc import abstractmethod
from bisect import bisect_left

from srearena.generators.workload.base import WorkloadEntry, WorkloadManager

STREAM_WORKLOAD_TIMEOUT = 60 * 1.5  # 1.5 minutes
STREAM_WORKLOAD_EPS = 10  # 5 seconds


class StreamWorkloadManager(WorkloadManager):
    """
    Stream-like workload manager
    """

    log_history: list[WorkloadEntry] = []
    last_log_time: float | None = None  # The timestamp inside the pod

    def __init__(self):
        super().__init__()

        self.last_log_time = None

    @abstractmethod
    def retrievelog(self, start_time: float | None = None) -> list[WorkloadEntry]:
        """
        Retrieve new logs. Like a stream, it should return only new logs since the last retrieval.
        """

        raise NotImplementedError("Subclasses must implement this method.")

    def _extractlog(self):
        """
        Stream-like log extraction.
        """
        while True:
            # In case of byte limits
            new_logs = self.retrievelog(self.last_log_time)

            if not new_logs:
                return

            if not sorted(new_logs, key=lambda x: x.time):
                raise ValueError("Logs are not sorted by time.")

            first_greater = 0
            if self.last_log_time is not None:
                while first_greater < len(new_logs) and new_logs[first_greater].time <= self.last_log_time:
                    first_greater += 1

            if first_greater < len(new_logs):
                self.log_history.extend(new_logs[first_greater:])
                self.last_log_time = new_logs[-1].time

    def collect(self, number=100, since_seconds=None) -> list[WorkloadEntry]:
        """
        Run the workload generator until collected data is sufficient.
        """
        if since_seconds is not None:
            if not isinstance(since_seconds, (int, float)):
                raise TypeError("since_seconds must be a int or float")
            if since_seconds > self.last_log_time:
                since_seconds = self.last_log_time

        # I put it here becuase the first run of it may be very late
        self._extractlog()

        collect_start_time = time.time()

        if since_seconds is None or self.last_log_time is None:
            start_entry = len(self.log_history)
        else:
            start_entry = bisect_left(
                self.log_history,
                self.last_log_time - since_seconds,
                key=lambda x: x.time if isinstance(x, WorkloadEntry) else x,
            )

        end_entry = start_entry

        accumulated_logs = 0

        while time.time() - collect_start_time < STREAM_WORKLOAD_TIMEOUT:
            while end_entry < len(self.log_history):
                accumulated_logs += self.log_history[end_entry].number
                end_entry += 1
            if accumulated_logs >= number:
                return self.log_history[start_entry:end_entry]
            time.sleep(5)
            self._extractlog()

        raise TimeoutError("Workload generator did not collect enough data within the timeout period.")

    def recent_entries(self, duration=30) -> list[WorkloadEntry]:
        """
        Return recently collected data within the given duration (seconds).
        """
        self._extractlog()
        start_time = self.last_log_time - duration
        start_entry = bisect_left(
            self.log_history,
            start_time,
            key=lambda x: x.time if isinstance(x, WorkloadEntry) else x,
        )
        return self.log_history[start_entry:]
