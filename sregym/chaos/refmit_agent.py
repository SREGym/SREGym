"""Scripted "agent" that applies a generated problem's reference mitigation.

Used by the validation phase to check solvability: it waits for the
mitigation stage, runs the artifact's ``mitigate.sh`` with the admin
kubeconfig, and submits. The conductor then grades the result with the same
SLO oracle the fixers face. Runs on the conductor host
(``container_isolation: false``), so use ``--internet-access open``.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

from sregym.chaos.artifact import discover

API = f"http://{os.getenv('API_HOSTNAME', 'localhost')}:{os.getenv('API_PORT', '8000')}"


def wait_for_mitigation(timeout_s: int = 1800) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{API}/status", timeout=10).json().get("stage") == "mitigation":
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError("conductor never reached the mitigation stage")


def main() -> int:
    problem_id = os.environ["CHAOS_ARTIFACT_ID"]
    artifact = discover()[problem_id]
    wait_for_mitigation()
    namespace = requests.get(f"{API}/get_app", timeout=10).json()["namespace"]
    started = time.time()
    result = artifact.run("mitigate.sh", namespace)
    record = {
        "problem_id": problem_id,
        "returncode": result.returncode,
        "seconds": round(time.time() - started),
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    out = Path(os.environ.get("SREGYM_CHAOS_REPORT_DIR", "results/chaos_gates"))
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{problem_id}.mitigate.{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(record, indent=2))
    print(f"[refmit] mitigate.sh exited {result.returncode}", flush=True)
    response = requests.post(f"{API}/submit", json={"solution": ""}, timeout=1200)
    print(f"[refmit] submit -> {response.status_code} {response.text[:300]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
