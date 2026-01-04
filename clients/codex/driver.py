"""
Codex agent driver for SREGym.
Entry point for running Codex agent on SREGym tasks.
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

from clients.codex.codex_agent import CodexAgent

logger = logging.getLogger("all.codex.driver")


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


def build_instruction(app_info: dict, problem_id: str) -> str:
    """
    Build the instruction string for Codex.

    Args:
        app_info: Application information from conductor
        problem_id: Problem identifier

    Returns:
        Instruction string to pass to Codex
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

Your task is to mitigate the issue in the application:
1. Diagnose the issue in the application
2. Identify the root cause
3. Implement a fix to resolve the problem
4. When you have successfully fixed the issue, submit by calling the conductor API at {get_api_base_url()}/submit

The submission endpoint expects a POST request with JSON body:
{{"solution": ""}}

For mitigation, you should submit with an empty solution string once you have applied the fix.
The submission will trigger an oracle that validates whether the issue has been resolved.

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
        return_code: Codex return code
        usage_metrics: Token usage metrics
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = logs_dir / f"codex_results_{problem_id}_{timestamp}.json"

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
    """Main entry point for Codex agent driver."""
    parser = argparse.ArgumentParser(description="Run Codex agent on SREGym tasks")
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("MODEL_ID", "claude-sonnet-4-5"),
        help="Model to use for Codex (default: from MODEL_ID env var or claude-sonnet-4-5)",
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default="./logs/codex",
        help="Directory to store logs (default: ./logs/codex)",
    )
    parser.add_argument(
        "--codex-home",
        type=str,
        default=None,
        help="Codex home directory (default: same as logs-dir)",
    )

    args = parser.parse_args()

    logger.info("=" * 80)
    logger.info("Starting Codex agent for SREGym")
    logger.info(f"Model: {args.model}")
    logger.info(f"Logs directory: {args.logs_dir}")
    logger.info("=" * 80)

    # Get problem information
    try:
        app_info = get_app_info()
        problem_id = get_problem_id()
    except Exception as e:
        logger.error(f"Failed to get problem information: {e}")
        sys.exit(1)

    # Build instruction
    instruction = build_instruction(app_info, problem_id)

    # Initialize Codex agent
    logs_dir = Path(args.logs_dir)
    codex_home = Path(args.codex_home) if args.codex_home else None

    agent = CodexAgent(
        logs_dir=logs_dir,
        model_name=args.model,
        codex_home=codex_home,
    )

    # Run Codex
    logger.info("Starting Codex execution...")
    return_code = agent.run(instruction)

    # Get usage metrics
    usage_metrics = agent.get_usage_metrics()

    # Save results
    save_results(logs_dir, problem_id, return_code, usage_metrics)

    # Log summary
    logger.info("=" * 80)
    logger.info("Codex execution completed")
    logger.info(f"Return code: {return_code}")
    logger.info(f"Usage metrics: {usage_metrics}")
    logger.info("=" * 80)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
