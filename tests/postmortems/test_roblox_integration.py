"""Opt-in real-container validation; each case owns and removes its volumes.

SREGYM_POSTMORTEM_INTEGRATION=1 pytest -q tests/postmortems/test_roblox_integration.py
"""

import concurrent.futures
import contextlib
import json
import os
import time
import urllib.error
import uuid

import pytest

from sregym.postmortems.roblox_consul.common import request
from sregym.postmortems.roblox_consul.runner import Run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SREGYM_POSTMORTEM_INTEGRATION") != "1", reason="requires Docker and opt-in"),
]


@pytest.mark.parametrize(
    "tier,mode,seed", [("small", "historical", 0), ("small", "intervention", 42), ("scaled", "historical", 7)]
)
def test_real_lifecycle(tier, mode, seed):
    run = Run("test-" + uuid.uuid4().hex[:10])
    try:
        run.up(tier, mode, seed)
        assert json.loads((run.root / "baseline.json").read_text())["passed"]
        run.inject()
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            request(run.url("control") + "/runner/evidence", "POST", {})
        assert unauthorized.value.code == 403
        with pytest.raises(urllib.error.HTTPError):
            run.ops("config", changes={"active": False})
        fault = run.grade(window=6, challenge=False)
        assert not fault["passed"]
        (run.root / "fault-grade.json").write_text(json.dumps(fault, indent=2))
        with pytest.raises(urllib.error.HTTPError):
            run.ops("metrics")
        if mode == "historical":
            run.ops("snapshot-restore")
            with pytest.raises(urllib.error.HTTPError):
                run.ops("metrics")
        result = run.oracle()
        assert result["passed"], result
        (run.root / "recovery-grade.json").write_text(json.dumps(result, indent=2))
        if tier == "small" and mode == "historical":
            # Cached good responses must not hide corruption in persistent data.
            run.dc(
                "exec",
                "-T",
                "database",
                "psql",
                "-U",
                "postgres",
                "-d",
                "platform",
                "-c",
                "UPDATE players SET coins=0 WHERE id=0",
            )
            corrupt = run.grade(window=6, challenge=False)
            assert not corrupt["checks"]["player_data_preserved"]
            (run.root / "corruption-grade.json").write_text(json.dumps(corrupt, indent=2))
            run.dc(
                "exec",
                "-T",
                "database",
                "psql",
                "-U",
                "postgres",
                "-d",
                "platform",
                "-c",
                "UPDATE players SET coins=1000 WHERE id=0",
            )
            # Reopening a cold fleet under demand must have actual consequences.
            run.ops("cache-flush")
            url = run.url("gateway")

            def cold_read(i):
                with contextlib.suppress(urllib.error.HTTPError):
                    request(f"{url}/join?player={i}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
                list(pool.map(cold_read, range(40)))
            before = request(url + "/metrics")["origin_overloads"]
            assert before > 0
            run.ops("config", changes={"admission_percent": 0})
            run.dc("restart", "gateway")
            for _ in range(30):
                try:
                    metrics = request(run.url("gateway") + "/metrics")
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                pytest.fail("gateway did not restart")
            assert metrics["origin_overloads"] >= before
            unsafe = run.oracle()
            assert not unsafe["checks"]["no_origin_overload"]
            assert unsafe["checks"]["sustained_success"]
            (run.root / "unsafe-recovery-grade.json").write_text(json.dumps(unsafe, indent=2))
    finally:
        if run.compose.exists():
            run.down()
