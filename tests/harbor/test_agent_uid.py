"""The Harbor agent's UID must not be one any workload runs as (docker/harbor/Dockerfile)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHARTS = [ROOT / "sregym", ROOT / "SREGym-applications"]


def test_no_workload_runs_as_the_agent():
    match = re.search(r"useradd -m -u (\d+)", (ROOT / "docker/harbor/Dockerfile").read_text())
    assert match, "the agent's UID is pinned in docker/harbor/Dockerfile"
    agent_uid = int(match.group(1))
    assert 1001 < agent_uid < 65536  # inside the 65536 IDs Sysbox and /etc/subuid map
    if not (ROOT / "SREGym-applications/astronomy-shop").is_dir():
        pytest.skip("SREGym-applications submodules are not checked out")
    pattern = re.compile(r"\b(?:runAsUser|runAsGroup|fsGroup):\s*(\d+)")
    used = {
        int(uid)
        for root in CHARTS
        for path in root.rglob("*")
        if path.suffix in {".yaml", ".yml", ".tpl"} and path.is_file()
        for uid in pattern.findall(path.read_text(errors="ignore"))
    }
    assert 1001 in used  # the scan finds the UIDs charts do use
    assert agent_uid not in used
