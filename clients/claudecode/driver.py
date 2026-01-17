"""
Claude Code agent driver for SREGym.
Entry point for running Claude Code agent on SREGym tasks.
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

from logger import init_logger

init_logger()

from clients.claudecode.claudecode_agent import ClaudeCodeAgent

logger = logging.getLogger("all.claudecode.driver")


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


def get_problem_id() -> str:
    """Get current problem ID from conductor API."""
    api_url = f"{get_api_base_url()}/get_problem"
    logger.info(f"Fetching problem ID from {api_url}")

    try:
        response = requests.get(api_url)
        response.raise_for_status()
        problem_data = response.json()
        problem_id = problem_data.get("problem_id")
        logger.info(f"Problem ID: {problem_id}")
        return problem_id
    except Exception as e:
        logger.error(f"Failed to get problem ID: {e}")
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

    logger.info(f"Waiting for conductor to reach submission-ready stage...")

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


def build_instruction(app_info: dict, problem_id: str) -> str:
    """
    Build the instruction string for Claude Code.

    Args:
        app_info: Application information from conductor
        problem_id: Problem identifier

    Returns:
        Instruction string to pass to Claude Code
    """
    app_name = app_info.get("app_name", "unknown")
    namespace = app_info.get("namespace", "default")
    descriptions = app_info.get("descriptions", "")

    # Build instruction similar to how it would be done in Harbor
    instruction = f"""You are an SRE agent tasked with diagnosing and fixing issues in a Kubernetes application.

Application: {app_name}
Namespace: {namespace}
Problem ID: {problem_id}

{descriptions}

WORKFLOW: You will perform TWO tasks in sequence:

TASK 1: DIAGNOSIS
- Investigate the application to detect any anomalies or issues
- Analyze metrics, logs, and traces
- When ready, submit a natural language description of the issue you found

TASK 2: MITIGATION
- Identify the root cause of the issue
- Implement a fix to resolve the problem
- When the fix is applied, submit to trigger validation

HOW TO SUBMIT:

The submission endpoint is: {get_api_base_url()}/submit

For DIAGNOSIS stage:
- Submit with a natural language description of the issue
- Example: POST {get_api_base_url()}/submit with JSON: {{"solution": "The frontend service is crashing due to missing environment variable"}}

For MITIGATION stage:
- Submit with an EMPTY STRING after you have applied the fix
- POST {get_api_base_url()}/submit with JSON: {{"solution": ""}}

Important:
- You have access to kubectl commands to inspect and modify resources in namespace '{namespace}'
- You can query metrics and traces through the available observability tools
- The conductor API is available at {get_api_base_url()}
"""

    logger.info(f"Built instruction:\n{instruction}")
    return instruction


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
        return_code: Claude Code return code
        usage_metrics: Token usage metrics
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = logs_dir / f"claudecode_results_{problem_id}_{timestamp}.json"

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
    """Main entry point for Claude Code agent driver."""
    parser = argparse.ArgumentParser(description="Run Claude Code agent on SREGym tasks")
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("MODEL_ID", "claude-sonnet-4-5"),
        help="Model to use for Claude Code (default: from MODEL_ID env var or claude-sonnet-4-5)",
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default="./logs/claudecode",
        help="Directory to store logs (default: ./logs/claudecode)",
    )
    parser.add_argument(
        "--sessions-dir",
        type=str,
        default=None,
        help="Claude Code sessions directory (default: logs-dir/sessions)",
    )
    parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Disable auto-installation of Claude Code CLI if not found",
    )

    args = parser.parse_args()

    logger.info("=" * 80)
    logger.info("Starting Claude Code agent for SREGym")
    logger.info(f"Model: {args.model}")
    logger.info(f"Logs directory: {args.logs_dir}")
    logger.info("=" * 80)

    # Check if Claude Code CLI is installed
    try:
        ClaudeCodeAgent.ensure_installed(auto_install=not args.no_auto_install)
    except RuntimeError as e:
        logger.error(f"Claude Code CLI installation check failed: {e}")
        sys.exit(1)

    # Wait for conductor to be ready
    try:
        stage = wait_for_ready_stage(timeout=300)
        logger.info(f"Conductor is ready at stage: {stage}")
    except TimeoutError as e:
        logger.error(f"Timeout waiting for conductor: {e}")
        sys.exit(1)

    # Get problem information
    try:
        app_info = get_app_info()
        problem_id = get_problem_id()
    except Exception as e:
        logger.error(f"Failed to get problem information: {e}")
        sys.exit(1)

    # Build instruction
    instruction = build_instruction(app_info, problem_id)

    # Initialize Claude Code agent
    logs_dir = Path(args.logs_dir)
    sessions_dir = Path(args.sessions_dir) if args.sessions_dir else None

    agent = ClaudeCodeAgent(
        logs_dir=logs_dir,
        model_name=args.model,
        sessions_dir=sessions_dir,
    )

    # Run Claude Code
    logger.info("Starting Claude Code execution...")
    return_code = agent.run(instruction)

    # Get usage metrics
    usage_metrics = agent.get_usage_metrics()

    # Save results
    save_results(logs_dir, problem_id, return_code, usage_metrics)

    # Log summary
    logger.info("=" * 80)
    logger.info("Claude Code execution completed")
    logger.info(f"Return code: {return_code}")
    logger.info(f"Usage metrics: {usage_metrics}")
    logger.info("=" * 80)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
