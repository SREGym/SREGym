"""Deterministic stage-submission probe run by the Copilot CLI agent."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

from clients.harness.problem_id import resolve_problem_id


def _api_base_url() -> str:
    host = os.getenv("API_HOSTNAME", "localhost")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{os.getenv('API_PORT', '8000')}"


def _status() -> str | None:
    response = requests.get(f"{_api_base_url()}/status", timeout=5)
    response.raise_for_status()
    return response.json().get("stage")


def _wait_for_stage(stages: set[str], timeout: int = 300) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        stage = _status()
        if stage in stages:
            return stage
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for stages {sorted(stages)}")


def _wait_for_stage_change(previous: str, timeout: int = 900) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        stage = _status()
        if stage != previous:
            return stage or ""
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for stage to change from {previous!r}")


def _submit(problem_id: str, stage: str) -> dict:
    solution = (
        f"Copilot CLI stage plumbing probe for {problem_id}: submitted during {stage}. "
        "This run validates benchmark stage progression and submission handling."
    )
    response = requests.post(f"{_api_base_url()}/submit", json={"solution": solution}, timeout=60)
    response.raise_for_status()
    print(f"[copilotcli-stage-probe] Submit response for {stage}: {response.text}", flush=True)
    return {
        "stage": stage,
        "solution": solution,
        "response": response.json(),
        "timestamp": datetime.now().isoformat(),
    }


def main() -> None:
    problem_id = resolve_problem_id()
    records = []
    stage = _wait_for_stage({"diagnosis", "mitigation", "done"}, timeout=300)
    print(f"[copilotcli-stage-probe] Initial stage: {stage}", flush=True)

    while stage in {"diagnosis", "mitigation"}:
        records.append(_submit(problem_id, stage))
        next_stage = _wait_for_stage_change(stage, timeout=1200)
        print(f"[copilotcli-stage-probe] Stage changed from {stage} to {next_stage}", flush=True)
        if next_stage == "tearing_down":
            stage = _wait_for_stage({"done"}, timeout=1200)
            break
        stage = next_stage

    logs_dir = Path(os.environ.get("AGENT_LOGS_DIR", ".")).resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "problem_id": problem_id,
        "submissions": records,
        "final_stage": stage,
        "timestamp": datetime.now().isoformat(),
    }
    evidence_path = logs_dir / "copilotcli_stage_evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2))
    print(f"[copilotcli-stage-probe] Evidence written to {evidence_path}", flush=True)


if __name__ == "__main__":
    main()
