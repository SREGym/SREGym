"""Host-launched Copilot CLI driver for SREGym plumbing probes."""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

from clients.harness.problem_id import resolve_problem_id
from logger import init_logger

init_logger()
logger = logging.getLogger("all.copilotcli.driver")


def _api_base_url() -> str:
    host = os.getenv("API_HOSTNAME", "localhost")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{os.getenv('API_PORT', '8000')}"


def _wait_for_ready_stage(timeout: int = 300) -> str:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = requests.get(f"{_api_base_url()}/status", timeout=5)
            response.raise_for_status()
            stage = response.json().get("stage")
            if stage in {"diagnosis", "mitigation"}:
                return stage
        except Exception as exc:
            logger.debug("Waiting for conductor status: %s", exc)
        time.sleep(1)
    raise TimeoutError(f"Conductor did not reach a submission-ready stage within {timeout}s")


def _build_prompt(command: str, *, source_probe: bool) -> str:
    objective = (
        "drive a deterministic source-edit, rebuild, redeploy, and submit flow"
        if source_probe
        else "drive deterministic submissions through every available benchmark stage"
    )
    return f"""You are running as the SREGym benchmark agent for a plumbing validation run.

Do not try to solve the benchmark problem. The benchmark is only validating that the Copilot CLI agent can
{objective}.

Run this exact command in the current repository and wait for it to finish:

{command}

Do not ask questions. Do not run unrelated commands. If the command fails, report the failure and exit non-zero.
"""


def main() -> None:
    logs_dir = Path(os.environ.get("AGENT_LOGS_DIR", "./logs/copilotcli")).resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)

    problem_id = resolve_problem_id()
    copilot = shutil.which("copilot")
    if not copilot:
        raise RuntimeError("GitHub Copilot CLI is not installed or not on PATH")

    stage = _wait_for_ready_stage()
    logger.info("Conductor is ready at stage %s for problem %s", stage, problem_id)

    source_path = os.environ.get("SREGYM_SOURCE_CODE_PATH")
    source_probe = problem_id == "auto_cassandra_20036" and bool(source_path)
    if source_probe:
        logger.info("Problem source path: %s", source_path)
        command = f"{sys.executable} -m clients.copilotcli.rebuild_redeploy_probe"
    else:
        command = f"{sys.executable} -m clients.copilotcli.stage_submit_probe"
    prompt = _build_prompt(command, source_probe=source_probe)
    prompt_path = logs_dir / "copilot_prompt.txt"
    prompt_path.write_text(prompt)

    model = os.environ.get("COPILOTCLI_MODEL", "auto")
    output_path = logs_dir / "copilotcli.txt"
    cmd = [
        copilot,
        "-C",
        str(Path.cwd()),
        "--prompt",
        prompt,
        "--model",
        model,
        "--allow-all",
        "--no-ask-user",
        "--no-custom-instructions",
        "--no-auto-update",
        "--autopilot",
        "--max-autopilot-continues",
        os.environ.get("COPILOTCLI_MAX_CONTINUES", "10"),
        "--log-dir",
        str(logs_dir / "copilot-logs"),
        "--log-level",
        os.environ.get("COPILOTCLI_LOG_LEVEL", "error"),
    ]

    logger.info("Starting Copilot CLI probe with model=%s", model)
    with output_path.open("w") as out:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        if process.stdout:
            for line in process.stdout:
                out.write(line)
                out.flush()
                print(line, end="", flush=True)
        process.wait()

    result = {
        "problem_id": problem_id,
        "timestamp": datetime.now().isoformat(),
        "return_code": process.returncode,
        "stage": stage,
        "source_path": source_path,
        "source_probe": source_probe,
        "copilot_output": str(output_path),
    }
    result_path = logs_dir / "copilotcli_results.json"
    result_path.write_text(json.dumps(result, indent=2))
    logger.info("Copilot CLI exited with return code %s", process.returncode)
    sys.exit(process.returncode)


if __name__ == "__main__":
    main()
