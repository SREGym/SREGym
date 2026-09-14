"""
Cursor agent driver for SREGym.
Entry point for running the Cursor CLI agent on SREGym tasks.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

# Add SREGym root to path
sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

from clients.cursor.cursor_agent import CursorAgent  # noqa: E402
from clients.harness.problem_id import resolve_problem_id  # noqa: E402
from clients.harness.workspace import append_workspace_hint  # noqa: E402

logger = logging.getLogger("all.cursor.driver")


def run_preflight() -> None:
    """Validate model + credentials by making a minimal Cursor CLI call."""
    import subprocess

    if not os.environ.get("CURSOR_API_KEY"):
        print("missing CURSOR_API_KEY")
        sys.exit(1)

    m = os.environ["AGENT_MODEL_ID"]
    command = ["agent", "--print", "say ok", "--model", m, "--output-format", "json", "--trust"]

    r = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    if r.returncode:
        print(r.stdout or r.stderr)
    sys.exit(r.returncode)


def get_api_base_url() -> str:
    """Get the conductor API base URL."""
    host = os.getenv("API_HOSTNAME", "localhost")
    port = os.getenv("API_PORT", "8000")
    return f"http://{host}:{port}"


def get_app_info() -> dict:
    """Get application info from conductor API."""
    api_url = f"{get_api_base_url()}/get_app"
    logger.info(f"Fetching app info from {api_url}")

    try:
        response = requests.get(api_url)
        response.raise_for_status()
        app_info = response.json()
        logger.info(f"App info: {app_info}")
        return app_info
    except Exception as e:
        logger.error(f"Failed to get app info: {e}")
        raise


def wait_for_ready_stage(timeout: int = 300) -> str:
    """
    Wait for conductor to reach a submission-ready stage (diagnosis or mitigation).

    Args:
        timeout: Maximum seconds to wait

    Returns:
        Current stage name

    Raises:
        TimeoutError: If timeout is reached before ready
    """
    import time

    api_url = f"{get_api_base_url()}/status"
    allowed_stages = {"diagnosis", "mitigation"}
    start_time = time.time()

    logger.info("Waiting for conductor to reach submission-ready stage...")

    while time.time() - start_time < timeout:
        try:
            response = requests.get(api_url)
            response.raise_for_status()
            status_data = response.json()
            stage = status_data.get("stage")

            if stage in allowed_stages:
                logger.info(f"Conductor ready at stage: {stage}")
                return stage
            else:
                logger.debug(f"Current stage: {stage}, waiting for {allowed_stages}...")
                time.sleep(1)

        except Exception as e:
            logger.debug(f"Error checking status: {e}, retrying...")
            time.sleep(1)

    raise TimeoutError(f"Conductor did not reach ready stage within {timeout} seconds")


def build_instruction(app_info: dict) -> str:
    """
    Build the instruction string for the Cursor agent.

    Args:
        app_info: Application information from conductor

    Returns:
        Instruction string to pass to the agent
    """
    app_name = app_info.get("app_name", "unknown")
    namespace = app_info.get("namespace", "default")
    descriptions = app_info.get("descriptions", "")

    instruction = f"""You are an SRE agent tasked with diagnosing and fixing issues in a Kubernetes application.

Application: {app_name}
Namespace: {namespace}

{descriptions}

CRITICAL: You are running in an AUTOMATED environment. Work autonomously and make all decisions yourself. DO NOT ask for user confirmation or approval. Proceed with the best solution based on your analysis.

WORKFLOW: You will perform TWO tasks in sequence:

TASK 1: DIAGNOSIS
- Investigate the application to detect any anomalies or issues
- Analyze metrics, logs, and traces
- When ready, submit a natural language description of the issue you found
- Your diagnosis is evaluated on whether you correctly identify the faulty components and root cause

TASK 2: MITIGATION
- Identify the root cause of the issue
- Implement a fix to resolve the problem autonomously (do not ask for confirmation)
- After applying the fix, YOU MUST submit with an empty string to trigger validation
- The submission is REQUIRED - do not exit without submitting
- Your mitigation is evaluated on whether the application is healthy after your changes
- Your fix is also evaluated on whether it addresses the root cause, not just the symptoms

HOW TO SUBMIT:

The submission endpoint is: {get_api_base_url()}/submit

For DIAGNOSIS stage:
- Submit with a natural language description of the issue
- Example: POST {get_api_base_url()}/submit with JSON: {{"solution": "The frontend service is crashing due to missing environment variable"}}

For MITIGATION stage:
- After applying your fix, YOU MUST submit with an EMPTY STRING
- POST {get_api_base_url()}/submit with JSON: {{"solution": ""}}
- This submission is MANDATORY - the conductor needs it to validate your fix

Important:
- You have access to kubectl commands to inspect and modify resources in namespace '{namespace}'
- You can query metrics and traces through the available observability tools
- The conductor API is available at {get_api_base_url()}
- Use shell commands directly (kubectl, curl, etc.) rather than asking to run them
"""

    logger.info(f"Built instruction:\n{instruction}")
    return append_workspace_hint(instruction, app_info)


def save_results(
    logs_dir: Path,
    problem_id: str,
    return_code: int,
    usage_metrics: dict,
) -> None:
    """
    Save run results to JSON file.

    Args:
        logs_dir: Directory containing logs
        problem_id: Problem identifier
        return_code: Cursor agent return code
        usage_metrics: Token usage metrics
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = logs_dir / f"cursor_results_{problem_id}_{timestamp}.json"

    results = {
        "problem_id": problem_id,
        "timestamp": timestamp,
        "return_code": return_code,
        "success": return_code == 0,
        "usage_metrics": usage_metrics,
    }

    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Saved results to {results_file}")


def main():
    """Main entry point for Cursor agent driver."""
    parser = argparse.ArgumentParser(description="Run Cursor agent on SREGym tasks")
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("AGENT_MODEL_ID", "auto"),
        help="Model to use for the Cursor CLI (default: from AGENT_MODEL_ID env var or 'auto')",
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default=os.environ.get("AGENT_LOGS_DIR", "./logs/cursor"),
        help="Directory to store logs (default: ./logs/cursor)",
    )
    parser.add_argument(
        "--problem-id",
        type=str,
        default=None,
        help="Problem ID for artifact naming (default: SREGYM_ARTIFACT_ID in benchmark runs)",
    )
    parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Disable auto-installation of the Cursor CLI if not found",
    )

    args = parser.parse_args()

    logger.info("=" * 80)
    logger.info("Starting Cursor agent for SREGym")
    logger.info(f"Model: {args.model}")
    logger.info(f"Logs directory: {args.logs_dir}")
    logger.info("=" * 80)

    try:
        CursorAgent.ensure_installed(auto_install=not args.no_auto_install)
    except RuntimeError as e:
        logger.error(f"Cursor CLI installation check failed: {e}")
        sys.exit(1)

    try:
        stage = wait_for_ready_stage(timeout=300)
        logger.info(f"Conductor is ready at stage: {stage}")
    except TimeoutError as e:
        logger.error(f"Timeout waiting for conductor: {e}")
        sys.exit(1)

    try:
        app_info = get_app_info()
    except Exception as e:
        logger.error(f"Failed to get app info: {e}")
        sys.exit(1)

    problem_id = resolve_problem_id(cli_problem_id=args.problem_id)
    logger.info(f"Problem ID (harness): {problem_id}")

    instruction = build_instruction(app_info)

    logs_dir = Path(args.logs_dir)
    agent = CursorAgent(logs_dir=logs_dir, model_name=args.model)

    logger.info("Starting Cursor agent execution...")
    return_code = agent.run(instruction)

    usage_metrics = agent.get_usage_metrics()

    save_results(logs_dir, problem_id, return_code, usage_metrics)

    logger.info("=" * 80)
    logger.info("Cursor agent execution completed")
    logger.info(f"Return code: {return_code}")
    logger.info(f"Usage metrics: {usage_metrics}")
    logger.info("=" * 80)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
