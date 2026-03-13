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

from rich.console import Console

from llm_backend.init_backend import load_model_config
from logger import init_logger
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import get_agent, list_agents
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.conductor_api import request_shutdown, run_api
from sregym.conductor.constants import StartProblemResult

LAUNCHER = AgentLauncher()
logger = logging.getLogger(__name__)
_driver_results: list[dict] = []


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m%d_%H%M")
    return formatted_datetime


def driver_loop(
    conductor: Conductor,
    problem_filter: str | None = None,
    agent_to_run: str | None = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
    agent_timeout: int = 1800,
    resume_csv: str | None = None,
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
        resume_csv: Path to a previous results CSV to resume from (skip completed problems).
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

        # Resume support: load completed problems from previous CSV and pre-seed results
        completed_problems: set[str] = set()
        if resume_csv:
            try:
                with open(resume_csv, newline="") as f:
                    reader = csv.DictReader(f)
                    resume_rows = list(reader)
                # Group by problem_id and count attempts
                from collections import Counter

                attempt_counts = Counter(r["problem_id"] for r in resume_rows)
                completed_problems = {pid for pid, count in attempt_counts.items() if count >= n_attempts}
                # Pre-seed all_results_for_agent with the resumed data
                for row in resume_rows:
                    all_results_for_agent.append({agent_to_run: [row]})
                console.log(
                    f"📋 Resuming from {resume_csv}: {len(completed_problems)} problems already done, skipping them"
                )
            except Exception as e:
                console.log(f"⚠️  Failed to load resume CSV: {e}")

        for pid in problem_ids:
            if pid in completed_problems:
                console.log(f"⏭️  Skipping already-completed problem: {pid}")
                continue

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

                assert agent_to_run is not None

                reg = get_agent(agent_to_run, path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml")
                if reg:
                    await LAUNCHER.ensure_started(reg)

                # Poll until grading completes, agent exits, or timeout
                agent_start_time = time.time()
                while conductor.submission_stage != "done":
                    # Check agent timeout
                    if time.time() - agent_start_time > agent_timeout:
                        console.log(f"⏰ Agent timeout ({agent_timeout}s) exceeded, killing agent")
                        LAUNCHER.cleanup_agent(agent_to_run)
                        break

                    # Check if agent process has exited
                    agent_proc = LAUNCHER._procs.get(agent_to_run)
                    if agent_proc:
                        agent_proc.proc.poll()
                        if agent_proc.proc.returncode is not None:
                            console.log(f"⚠️  Agent process exited with return code {agent_proc.proc.returncode}")
                            # Wait for any in-progress background evaluation to finish
                            eval_wait = 0
                            while eval_wait < 120 and conductor.submission_stage != "done":
                                await asyncio.sleep(1)
                                eval_wait += 1
                            break
                    await asyncio.sleep(1)

                console.log(f"✅ Completed {pid}: results={conductor.results}")

                # Wait for agent process to complete naturally before cleanup
                # This allows the agent to finish saving trajectories and other cleanup tasks
                if not use_external_harness:
                    agent_proc = LAUNCHER._procs.get(agent_to_run)
                    if agent_proc:
                        console.log("⏳ Waiting for agent process to complete...")
                        timeout = 60  # seconds
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


def _run_driver_and_shutdown(
    conductor: Conductor,
    problem_filter: str | None = None,
    agent_to_run: str | None = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
    agent_timeout: int = 1800,
    resume_csv: str | None = None,
):
    """Run the benchmark driver, stash results, then tell the API to exit."""
    try:
        results = driver_loop(
            conductor,
            problem_filter=problem_filter,
            agent_to_run=agent_to_run,
            use_external_harness=use_external_harness,
            n_attempts=n_attempts,
            agent_timeout=agent_timeout,
            resume_csv=resume_csv,
        )
        global _driver_results
        _driver_results = results
    except Exception:
        logger.exception("Driver thread crashed")
    finally:
        LAUNCHER.cleanup_all()
        request_shutdown()


def main(args):
    # set up the logger
    init_logger()

    agent_model = args.model
    judge_model = args.judge_model or args.model

    # Initialize Noise Manager if --noise flag is set
    nm = None
    if args.noise:
        try:
            from sregym.generators.noise.manager import get_noise_manager

            nm = get_noise_manager()
            logger.info("Noise injection enabled.")
        except Exception as e:
            logger.warning(f"Failed to initialize noise manager: {e}")

    available_models = list(load_model_config().keys())

    # Always validate judge model
    if judge_model not in available_models:
        logger.error(
            f"❌ Judge model '{judge_model}' not found in llm_backend/configs.yaml. Available: {available_models}"
        )
        sys.exit(1)

    # Validate agent model for stratus
    if args.agent in ["stratus"] and agent_model not in available_models:
        logger.error(
            f"❌ Agent model '{agent_model}' not found in llm_backend/configs.yaml. Available: {available_models}"
        )
        sys.exit(1)

    # Push to env so downstream code picks it up
    os.environ["AGENT_MODEL_ID"] = agent_model
    os.environ["JUDGE_MODEL_ID"] = judge_model
    os.environ["API_HOSTNAME"] = "0.0.0.0"
    os.environ["API_PORT"] = "8000"
    os.environ["MCP_SERVER_PORT"] = "9954"
    os.environ["MCP_SERVER_URL"] = "http://127.0.0.1:9954"

    logger.info(f"🔧 Config — agent: {args.agent}, agent_model: {agent_model}, judge_model: {judge_model}")

    conductor_config = ConductorConfig(deploy_loki=not args.use_external_harness)
    conductor = Conductor(config=conductor_config)

    # Only build/check agent container image if the agent requires it
    agent_reg = (
        get_agent(args.agent, path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml")
        if args.agent
        else None
    )
    if not agent_reg or agent_reg.container_isolation:
        LAUNCHER.enable_container_isolation(force_build=args.force_build)

    # Start the driver in the background; it will call request_shutdown() when finished
    driver_thread = threading.Thread(
        target=_run_driver_and_shutdown,
        args=(
            conductor,
            args.problem,
            args.agent,
            args.use_external_harness,
            args.n_attempts,
            args.agent_timeout,
            args.resume,
        ),
        name="driver",
        daemon=True,
    )
    driver_thread.start()

    # Start the Conductor HTTP API in the MAIN thread (blocking)
    try:
        run_api(conductor)
    except KeyboardInterrupt:
        # If interrupted, still try to shut down cleanly
        LAUNCHER.cleanup_all()
        request_shutdown()
    finally:
        # Stop any remaining agent containers/processes
        LAUNCHER.cleanup_all()

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
    results = _driver_results

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
        default="stratus",
        help="Agent to run (default: stratus)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5",
        help="Model for both agent and judge (default: gpt-5)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help="Model for the LLM-as-a-judge evaluator (defaults to --model if not set)",
    )
    parser.add_argument(
        "--use-external-harness", action="store_true", help="For use in external harnesses, deploy the fault and exit."
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Enable transient noise injection via Chaos Mesh during problem runs",
    )
    parser.add_argument(
        "--n-attempts",
        type=int,
        default=1,
        help="Number of attempts to run each problem (default: 1)",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Force rebuild the agent Docker image even if it already exists (use after updating dependencies or build scripts)",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=1800,
        help="Agent timeout in seconds after deployment (default: 1800)",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Resume from a previous results CSV file. Problems already in the CSV will be skipped.",
    )
    args = parser.parse_args()

    # Validate that n_attempts is positive
    if args.n_attempts is not None and args.n_attempts < 1:
        parser.error("--n-attempts must be a positive integer")

    main(args)
