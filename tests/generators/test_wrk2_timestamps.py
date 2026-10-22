"""Workload timestamps must have the same meaning in every host timezone."""

import os
import time
from datetime import UTC, datetime

import pytest

from sregym.generators.workload.wrk2 import Wrk2WorkloadManager


@pytest.mark.parametrize("zone", ["UTC", "America/Chicago", "Asia/Tokyo"])
@pytest.mark.parametrize("stamp", ["2026-10-15T03:00:00.123456789Z", "2026-10-15T03:00:00.123456+00:00"])
def test_parse_round_timestamp_is_utc(zone, stamp):
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        logs = [
            {"time": stamp, "content": line}
            for line in (
                "Running wrk2",
                "-" * 40,
                " 1000 requests in 10.00s, 1MB read",
                "Requests/sec: 100",
            )
        ]
        entry = Wrk2WorkloadManager._parse_log(None, logs)
        expected = datetime(2026, 10, 15, 3, microsecond=123456, tzinfo=UTC).timestamp()
        assert entry.time == expected
        assert entry.number == 1000
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
