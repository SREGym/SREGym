import argparse
import asyncio
import csv
import logging
import multiprocessing
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from rich.console import Console

from logger import init_logger
from mcp_server.configs.load_all_cfg import mcp_server_cfg
from mcp_server.sregym_mcp_server import app as mcp_app
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import get_agent, list_agents
from sregym.conductor.conductor import Conductor
from sregym.conductor.conductor_api import request_shutdown, run_api
from sregym.conductor.constants import StartProblemResult

LAUNCHER = AgentLauncher()
logger = logging.getLogger(__name__)


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m-%d_%H-%M")
    return formatted_datetime


def driver_loop(
    conductor: Conductor, problem_filter: str = None, agent_to_run: str = "stratus", use_external_harness: bool = False
):
    """
    Deploy each problem and wait for HTTP grading via POST /submit.
    Returns a list of flattened dicts with results per problem.

    Args:
        conductor: The Conductor instance
        problem_filter: Optional problem ID to run. If specified, only this problem will be run.
        use_external_harness: If True, inject fault and exit without running evaluation logic.
    """

    async def driver():
        console = Console()
        # give the API a moment to bind
        await asyncio.sleep(1)
        agents_to_start = list_agents(path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml").keys()
        all_results = []

        if agent_to_run is not None:
            if agent_to_run not in agents_to_start:
                console.log(f"⚠️ Agent '{agent_to_run}' not found in registry. Available agents: {agents_to_start}")
                sys.exit(1)
            else:
                agents_to_start = [agent_to_run]

        for agent_name in agents_to_start:
            console.log(f"Starting agent now: {agent_name}")
            conductor.register_agent(agent_name)
            all_results_for_agent = []

            # Get all problem IDs and filter if needed
            problem_ids = conductor.problems.get_problem_ids()
            if problem_filter:
                if problem_filter not in problem_ids:
                    console.log(
                        f"⚠️  Problem '{problem_filter}' not found in registry. Available problems: {problem_ids}"
                    )
                    sys.exit(1)
                problem_ids = [problem_filter]
                console.log(f"🎯 Running single problem: {problem_filter}")

            for pid in problem_ids:
                console.log(f"\n🔍 Starting problem: {pid}")

                conductor.problem_id = pid

                result = await conductor.start_problem()
                if result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
                    console.log(f"⏭️  Skipping problem '{pid}': requires Khaos but running on emulated cluster")
                    continue

                # If using external harness, fault is injected - exit now
                if use_external_harness:
                    console.log(f"✅ Fault injected for problem '{pid}'. Exiting for external harness.")
                    return []

                if not use_external_harness:
                    reg = get_agent(agent_name, path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml")
                    if reg:
                        await LAUNCHER.ensure_started(reg)

                # Poll until grading completes
                while conductor.submission_stage != "done":
                    await asyncio.sleep(1)

                console.log(f"✅ Completed {pid}: results={conductor.results}")

                snapshot = {"problem_id": pid}
                for stage, outcome in conductor.results.items():
                    if isinstance(outcome, dict):
                        for k, v in outcome.items():
                            snapshot[f"{stage}.{k}"] = v
                    else:
                        snapshot[stage] = outcome
                all_results_for_agent.append(snapshot)

                fieldnames = sorted({key for row in all_results_for_agent for key in row.keys()})
                current_date_time = get_current_datetime_formatted()
                csv_path = f"{agent_name}_{current_date_time}_{pid}_results.csv"
                with open(csv_path, "w", newline="") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(all_results_for_agent)
                logger.info(f"✅ Problem {pid} for agent {agent_name} complete! Results written to {csv_path}")
            entry_for_agent = {agent_name: all_results_for_agent}
            all_results.append(entry_for_agent)

        return all_results

    return asyncio.run(driver())


def start_mcp_server_after_api():
    # Small delay so the main API binds first (avoid port races if clients hit MCP immediately)
    time.sleep(1.0)

    host = "0.0.0.0" if mcp_server_cfg.expose_server else "127.0.0.1"
    port = mcp_server_cfg.mcp_server_port

    config = uvicorn.Config(
        app=mcp_app,
        host=host,
        port=port,
        log_level="info",
    )
    # IMPORTANT: we're not in the main thread
    config.install_signal_handlers = False

    server = uvicorn.Server(config)
    # This call blocks *this* thread; it's fine because we're daemonizing the thread
    server.run()


def _run_driver_and_shutdown(
    conductor: Conductor, problem_filter: str = None, agent_to_run: str = "stratus", use_external_harness: bool = False
):
    """Run the benchmark driver, stash results, then tell the API to exit."""
    results = driver_loop(
        conductor, problem_filter=problem_filter, agent_to_run=agent_to_run, use_external_harness=use_external_harness
    )
    setattr(main, "results", results)
    # ⬇️ Ask the API server (running in main thread) to stop so we can write CSV
    request_shutdown()


def main(args):
    # set up the logger
    init_logger()

    # Initialize Noise Manager if config is provided or default config exists
    nm = None
    noise_config_path = args.noise_config
    default_noise_config = "sregym/generators/noise/noise_config.yaml"

    # Use default path if no argument provided but default file exists
    if not noise_config_path and os.path.exists(default_noise_config):
        noise_config_path = default_noise_config

    if noise_config_path:
        try:
            from sregym.generators.noise.manager import get_noise_manager

            nm = get_noise_manager()
            nm.load_config(noise_config_path)
            logger.info(f"✅ Noise manager initialized with config: {noise_config_path}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to initialize noise manager: {e}")

    os.environ["MODEL_ID"] = args.model

    conductor = Conductor()

    # Start the driver in the background; it will call request_shutdown() when finished
    driver_thread = threading.Thread(
        target=_run_driver_and_shutdown,
        args=(conductor, args.problem, args.agent, args.use_external_harness),
        name="driver",
        daemon=True,
    )
    driver_thread.start()

    # Start the MCP server in the background (lets the main thread run the Conductor API)
    if not args.use_external_harness:  # No need for MCP if using external harness
        mcp_thread = threading.Thread(
            target=start_mcp_server_after_api,
            name="mcp-server",
            daemon=True,
        )
        mcp_thread.start()

    # Start the Conductor HTTP API in the MAIN thread (blocking)
    try:
        run_api(conductor)
    except KeyboardInterrupt:
        # If interrupted, still try to shut down cleanly
        request_shutdown()
    finally:
        # Stop noise manager if it was initialized
        if nm:
            try:
                logger.info("Stopping noise manager...")
                nm.stop()
            except Exception as e:
                logger.error(f"⚠️ Error stopping noise manager: {e}")

        # Give driver a moment to finish setting results
        driver_thread.join(timeout=5)

    # When API shuts down, collect results from driver
    results = getattr(main, "results", [])

    if results:
        aggregated = {}
        for entry in results:
            for agent_name, agent_rows in entry.items():
                aggregated.setdefault(agent_name, []).extend(agent_rows)

        for agent_name, agent_results in aggregated.items():
            fieldnames = sorted({key for row in agent_results for key in row.keys()})
            current_date_time = get_current_datetime_formatted()
            csv_path = f"{current_date_time}_{agent_name}_ALL_results.csv"
            with open(csv_path, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(agent_results)
            logger.info(f"✅ Benchmark complete! Results for {agent_name} written to {csv_path}")
    else:
        logger.warning("⚠️ No results to write.")

    if __name__ == "__main__":
        # separate run, use exit
        sys.exit(0)
    else:
        # function call run, return results
        return results


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Run SREGym benchmark suite")
    parser.add_argument(
        "--problem",
        type=str,
        default=None,
        help="Run only a specific problem by its ID (e.g., 'target_port')",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default=None,
        help="Run only a specific agent by its name (e.g., 'stratus')",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o",
        help="Run only a specific model backend (e.g., 'gpt-4o', 'gemini-2.5-pro', 'claude-sonnet-4', 'moonshot')",
    )
    parser.add_argument(
        "--use-external-harness", action="store_true", help="For use in external harnesses, deploy the fault and exit."
    )
    parser.add_argument(
        "--noise-config",
        type=str,
        default=None,
        help="Path to noise configuration YAML file",
    )
    args = parser.parse_args()

    main(args)
