"""Test the legacy MySQL limit guard without changing the test runner's limits."""

import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker/train-ticket/mysql-nofile-entrypoint.sh"


@pytest.mark.parametrize(
    "limit,expected",
    [("20480", ""), ("655360", ""), ("1073741816", "limit -S -n 655360\n"), ("unlimited", "limit -S -n 655360\n")],
)
def test_only_excessive_limits_are_lowered_and_arguments_are_preserved(limit, expected):
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
            ulimit() {
                if [ "$#" = 1 ]; then printf '%s\n' "$MYSQL_TEST_LIMIT";
                else printf 'limit %s\n' "$*"; fi
            }
            script="$1"
            set -- printf '%s\n' 'argument with spaces' 'second argument'
            . "$script"
            """,
            "test-entrypoint",
            str(ENTRYPOINT),
        ],
        env={**os.environ, "MYSQL_TEST_LIMIT": limit},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == expected + "argument with spaces\nsecond argument\n"
