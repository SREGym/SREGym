import argparse
import asyncio
import csv
import importlib
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console

from logger import init_logger
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import get_agent, list_agents
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.conductor_api import request_shutdown, run_api
from sregym.conductor.constants import StartProblemResult
from sregym.service.container_runner import ContainerRunner, ExecInput

LAUNCHER = AgentLauncher()
logger = logging.getLogger(__name__)
_driver_results: list[dict] = []
_driver_base_dir: Path | None = None


def run_preflight_check(
    agent_name: str,
    container_runner: ContainerRunner | None = None,
    install_script: str | None = None,
) -> None:
    """Run the agent's pre-flight check inside the container."""

    # Agents that need pre-flight check
    agent_driver_modules: dict[str, str] = {
        "stratus": "clients.stratus.stratus_agent.driver.driver",
        "claudecode": "clients.claudecode.driver",
        "codex": "clients.codex.driver",
    }

    module_path = agent_driver_modules.get(agent_name)
    if not module_path:
        return

    driver_mod = importlib.import_module(module_path)
    if not hasattr(driver_mod, "run_preflight"):
        return

    if container_runner is None:
        logger.warning(f"⚠️  No container runner — skipping pre-flight check for '{agent_name}'")
        return

    check_cmd = f"python3 -c 'from {module_path} import run_preflight; run_preflight()'"
    if install_script:
        check_cmd = f"/opt/sregym/install-scripts/{install_script} > /dev/null 2>&1 && {check_cmd}"

    logger.info(f"🔍 Running pre-flight check for '{agent_name}'...")
    result = container_runner.run_sync(ExecInput(command=check_cmd, label="preflight", timeout=180))
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        logger.error(f"❌ Pre-flight check failed for '{agent_name}'")
        sys.exit(1)

    logger.info(f"✅ Pre-flight check passed for '{agent_name}'")


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

        base_dir = Path("results") / get_current_datetime_formatted()
        base_dir.mkdir(parents=True, exist_ok=True)
        global _driver_base_dir
        _driver_base_dir = base_dir
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

                # Retry start_problem up to 3 times to handle transient deploy failures
                max_deploy_retries = 3
                result = None
                for deploy_attempt in range(1, max_deploy_retries + 1):
                    try:
                        result = await conductor.start_problem()
                        break  # Success — exit retry loop
                    except Exception as e:
                        console.log(
                            f"❌ start_problem failed for '{pid}' "
                            f"(deploy attempt {deploy_attempt}/{max_deploy_retries}): {e}"
                        )
                        if deploy_attempt < max_deploy_retries:
                            console.log("🧹 Cleaning up before retry...")
                            try:
                                conductor._finish_problem()
                            except Exception as cleanup_err:
                                console.log(f"⚠️  Cleanup error (non-fatal): {cleanup_err}")
                            console.log(f"🔄 Retrying start_problem for '{pid}'...")
                        else:
                            console.log(
                                f"⛔ All {max_deploy_retries} deploy attempts failed for '{pid}', skipping this attempt"
                            )
                            result = None

                if result is None:
                    raise RuntimeError(f"All {max_deploy_retries} deploy attempts failed for problem '{pid}'")

                if result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
                    console.log(f"⏭️  Skipping problem '{pid}': requires Khaos but running on emulated cluster")
                    break  # Skip to next problem

                # If using external harness, fault is injected - exit now
                if use_external_harness:
                    console.log(f"✅ Fault injected for problem '{pid}'. Exiting for external harness.")
                    return []

                assert agent_to_run is not None

                # Create the run directory and point the agent at it before launch
                run_dir = base_dir / agent_to_run / pid / f"run_{attempt}"
                run_dir.mkdir(parents=True, exist_ok=True)
                os.environ["AGENT_LOGS_DIR"] = str(run_dir.resolve())

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

                        # Record timeout in results so downstream CSV captures the failure
                        conductor.results["timed_out"] = True
                        conductor.results["agent_timeout_seconds"] = agent_timeout

                        # Trigger conductor cleanup (fault recovery, teardown) so the
                        # next problem starts from a clean state.
                        console.log("🧹 Running conductor cleanup after agent timeout...")
                        conductor._finish_problem()

                        break

                    # Check if agent process has exited
                    agent_proc = LAUNCHER._procs.get(agent_to_run)
                    if agent_proc:
                        agent_proc.proc.poll()
                        if agent_proc.proc.returncode is not None:
                            console.log(f"⚠️  Agent process exited with return code {agent_proc.proc.returncode}")
                            # Wait for the conductor's background evaluation to finish.
                            # await the conductor's submit_future
                            if conductor._submit_future is not None and not conductor._submit_future.done():
                                console.log("⏳ Waiting for conductor evaluation to complete...")
                                try:
                                    await asyncio.wait_for(
                                        asyncio.wrap_future(conductor._submit_future),
                                        timeout=300,
                                    )
                                except TimeoutError:
                                    console.log("⚠️  Conductor evaluation did not finish within 300s")
                                except Exception as e:
                                    console.log(f"⚠️  Conductor evaluation raised: {e}")
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

                # run_dir was created above before agent launch; write per-attempt CSV into it
                attempt_path = run_dir / f"{pid}_results.csv"
                with open(attempt_path, "w", newline="") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerow(snapshot)

                logger.info(
                    f"⏳ Attempt {attempt} of {n_attempts} for problem {pid} complete - Intermediate results written to {tmp_path}"
                )

                if attempt == n_attempts:
                    final_csv_path = base_dir / agent_to_run / pid / f"{pid}_{agent_to_run}_results.csv"
                    os.replace(tmp_path, final_csv_path)
                    logger.info(
                        f"✅ Problem {pid} for agent {agent_to_run} complete! Results written to {final_csv_path}"
                    )

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

    if args.noise:
        logger.info("Noise injection enabled.")

    # Push to env so downstream code picks it up
    os.environ["AGENT_MODEL_ID"] = agent_model
    os.environ["JUDGE_MODEL_ID"] = judge_model
    os.environ["API_HOSTNAME"] = "0.0.0.0"
    os.environ["API_PORT"] = "8000"
    os.environ["MCP_SERVER_PORT"] = "9954"
    os.environ["MCP_SERVER_URL"] = "http://127.0.0.1:9954"

    logger.info(f"🔧 Config — agent: {args.agent}, agent_model: {agent_model}, judge_model: {judge_model}")

    # Only build/check agent container image if the agent requires it
    agent_reg = (
        get_agent(args.agent, path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml")
        if args.agent
        else None
    )
    if not agent_reg or agent_reg.container_isolation:
        LAUNCHER.enable_container_isolation(force_build=args.force_build)

    # Pre-flight check — makes a real (minimal) API call inside the agent
    # container to validate model and credentials in one shot.
    run_preflight_check(
        args.agent,
        container_runner=LAUNCHER._container_runner,
        install_script=agent_reg.install_script if agent_reg else None,
    )

    conductor_config = ConductorConfig(deploy_loki=not args.use_external_harness, enable_noise=args.noise)
    conductor = Conductor(config=conductor_config)

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

        # Stop noise manager if it was enabled
        if args.noise:
            try:
                from sregym.generators.noise.manager import get_noise_manager

                logger.info("Stopping noise manager...")
                get_noise_manager().stop()
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
            out_dir = _driver_base_dir if _driver_base_dir else Path("results")
            csv_path = out_dir / f"{agent_name}_ALL_results.csv"
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
        help="LiteLLM model string (e.g. anthropic/claude-sonnet-4-6-20250627, gpt-5, gemini/gemini-2.5-pro)",
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
