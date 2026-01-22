import argparse
import asyncio
import csv
import logging
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
    formatted_datetime = now.strftime("%m%d_%H%M")
    return formatted_datetime


def driver_loop(
    conductor: Conductor,
    problem_filter: str = None,
    agent_to_run: str = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
):
    """
    Deploy each problem and wait for HTTP grading via POST /submit.
    Returns a list of flattened dicts with results per problem.

    Args:
        conductor: The Conductor instance
        problem_filter: Optional problem ID to run. If specified, only this problem will be run.
        agent_to_run: Agent name to run (required unless use_external_harness is True).
        use_external_harness: If True, inject fault and exit without running evaluation logic.
        n_attempts: Number of end-to-end attempts to run each problem.
    """

    async def driver():
        console = Console()
        # give the API a moment to bind
        await asyncio.sleep(1)

        # Verify agent exists in registry (skip if using external harness)
        if not use_external_harness:
            available_agents = list_agents(path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml").keys()
            if agent_to_run not in available_agents:
                console.log(f"⚠️ Agent '{agent_to_run}' not found in registry. Available agents: {available_agents}")
                sys.exit(1)

            console.log(f"Starting agent now: {agent_to_run}")
            conductor.register_agent(agent_to_run)

            # Start K8s API proxy to hide chaos engineering namespaces from the agent
            console.log("🔒 Starting Kubernetes API proxy to hide chaos namespaces...")
            conductor.start_k8s_proxy()
            LAUNCHER.set_agent_kubeconfig(conductor.get_agent_kubeconfig_path())

        all_results_for_agent = []

        # Get all problem IDs and filter if needed
        problem_ids = conductor.problems.get_problem_ids()
        all_problem_ids = conductor.problems.get_problem_ids(all=True)
        if problem_filter:
            if problem_filter not in all_problem_ids:
                console.log(f"⚠️  Problem '{problem_filter}' not found in registry. Available problems: {problem_ids}")
                sys.exit(1)
            problem_ids = [problem_filter]
            console.log(f"🎯 Running single problem: {problem_filter}")

        # sanity check: are there any specified problem ids that do not exist in the registry?
        unknown_problem_ids = set(problem_ids) - set(all_problem_ids)
        if unknown_problem_ids:
            console.log(
                f"⚠️  These problem ids do not exist in the registry and they will be skipped: {unknown_problem_ids}"
            )
        for unknown_problem_id in unknown_problem_ids:
            problem_ids.remove(unknown_problem_id)

        for pid in problem_ids:
            conductor.problem_id = pid

            # Keep a record of results for this problem in a temp file in case an attempt fails
            tmp_path = f"_running_{pid}_{agent_to_run}_results.csv"

            for attempt in range(1, n_attempts + 1):
                console.log(f"\n🔍 Starting problem: {pid} (Attempt {attempt} of {n_attempts})")

                result = await conductor.start_problem()
                if result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
                    console.log(f"⏭️  Skipping problem '{pid}': requires Khaos but running on emulated cluster")
                    break  # Skip to next problem

                # If using external harness, fault is injected - exit now
                if use_external_harness:
                    console.log(f"✅ Fault injected for problem '{pid}'. Exiting for external harness.")
                    return []

                if not use_external_harness:
                    reg = get_agent(agent_to_run, path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml")
                    if reg:
                        await LAUNCHER.ensure_started(reg)

                # Poll until grading completes or agent exits
                while conductor.submission_stage != "done":
                    # Check if agent process has exited
                    agent_proc = LAUNCHER._procs.get(agent_to_run)
                    if agent_proc:
                        agent_proc.proc.poll()
                        if agent_proc.proc.returncode is not None:
                            console.log(f"⚠️  Agent process exited with return code {agent_proc.proc.returncode}")
                            break
                    await asyncio.sleep(1)

                console.log(f"✅ Completed {pid}: results={conductor.results}")

                # Wait for agent process to complete naturally before cleanup
                # This allows the agent to finish saving trajectories and other cleanup tasks
                if not use_external_harness:
                    agent_proc = LAUNCHER._procs.get(agent_to_run)
                    if agent_proc:
                        console.log("⏳ Waiting for agent process to complete...")
                        timeout = 30  # seconds
                        elapsed = 0
                        while elapsed < timeout:
                            agent_proc.proc.poll()
                            if agent_proc.proc.returncode is not None:
                                console.log(f"✅ Agent process completed with return code {agent_proc.proc.returncode}")
                                break
                            await asyncio.sleep(1)
                            elapsed += 1
                        else:
                            console.log(f"⚠️  Agent process did not complete within {timeout}s, will force cleanup")

                snapshot = {
                    "problem_id": pid,
                    "attempt": attempt,
                }

                for stage, outcome in conductor.results.items():
                    if isinstance(outcome, dict):
                        for k, v in outcome.items():
                            snapshot[f"{stage}.{k}"] = v
                    else:
                        snapshot[stage] = outcome

                all_results_for_agent.append(snapshot)

                fieldnames = sorted({key for row in all_results_for_agent for key in row})

                with open(tmp_path, "w", newline="") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(all_results_for_agent)

                logger.info(
                    f"⏳ Attempt {attempt} of {n_attempts} for problem {pid} complete - Intermediate results written to {tmp_path}"
                )

                if attempt == n_attempts:
                    current_date_time = get_current_datetime_formatted()
                    csv_path = f"{current_date_time}_{pid}_{agent_to_run}_results.csv"
                    os.replace(tmp_path, csv_path)
                    logger.info(f"✅ Problem {pid} for agent {agent_to_run} complete! Results written to {csv_path}")

                # Cleanup agent process so a fresh one can be started for the next problem
                if not use_external_harness:
                    LAUNCHER.cleanup_agent(agent_to_run)
                    console.log(f"🧹 Cleaned up agent process for {agent_to_run}")

        # Stop K8s API proxy when all problems are done
        if not use_external_harness:
            console.log("🔓 Stopping Kubernetes API proxy...")
            conductor.stop_k8s_proxy()

        return [{agent_to_run: all_results_for_agent}]

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
    conductor: Conductor,
    problem_filter: str = None,
    agent_to_run: str = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
):
    """Run the benchmark driver, stash results, then tell the API to exit."""
    results = driver_loop(
        conductor,
        problem_filter=problem_filter,
        agent_to_run=agent_to_run,
        use_external_harness=use_external_harness,
        n_attempts=n_attempts,
    )
    main.results = results
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
        args=(conductor, args.problem, args.agent, args.use_external_harness, args.n_attempts),
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
            fieldnames = sorted({key for row in agent_results for key in row})
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
        help="Agent to run by its name (e.g., 'stratus')",
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
    parser.add_argument(
        "--n-attempts",
        type=int,
        default=1,
        help="Number of attempts to run each problem (default: 1)",
    )
    args = parser.parse_args()

    # Validate that --agent is provided when not using external harness
    if not args.use_external_harness and args.agent is None:
        parser.error("--agent is required when --use-external-harness is not set")

    # Validate that n_attempts is positive
    if args.n_attempts < 1:
        parser.error("--n-attempts must be a positive integer")

    main(args)
