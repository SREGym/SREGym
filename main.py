import asyncio
import csv
import logging
import multiprocessing
import os
import sys
import threading
import time
from datetime import datetime

import uvicorn
from rich.console import Console

from dashboard.dashboard_app import SREGymDashboardServer
from dashboard.proxy import LogProxy
from logger import init_logger
from mcp_server.configs.load_all_cfg import mcp_server_cfg
from mcp_server.sregym_mcp_server import app as mcp_app
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import get_agent, list_agents
from sregym.conductor.conductor import Conductor
from sregym.conductor.conductor_api import request_shutdown, run_api
from sregym.conductor.constants import StartProblemResult

LAUNCHER = AgentLauncher()


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m-%d_%H-%M")
    return formatted_datetime


def driver_loop(conductor: Conductor):
    """
    Deploy each problem and wait for HTTP grading via POST /submit.
    Returns a list of flattened dicts with results per problem.
    """

    async def driver():
        console = Console()
        # give the API a moment to bind
        await asyncio.sleep(1)
        agents_to_start = list_agents()
        all_results = []
        for agent_name in agents_to_start.keys():
            console.log(f"Starting agent now: {agent_name}")
            conductor.register_agent(agent_name)
            all_results_for_agent = []
            for pid in conductor.problems.get_problem_ids():
                console.log(f"\n🔍 Starting problem: {pid}")

                conductor.problem_id = pid

                result = await conductor.start_problem()
                if result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
                    console.log(f"⏭️  Skipping problem '{pid}': requires Khaos but running on emulated cluster")
                    continue

                reg = get_agent(agent_name)
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
                csv_path = f"{agent_name}_{current_date_time}_arena_{pid}_results.csv"
                with open(csv_path, "w", newline="") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(all_results_for_agent)
                print(f"✅ Problem {pid} for agent {agent_name} complete! Results written to {csv_path}")
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


def _run_driver_and_shutdown(conductor: Conductor):
    """Run the benchmark driver, stash results, then tell the API to exit."""
    results = driver_loop(conductor)
    setattr(main, "results", results)
    # ⬇️ Ask the API server (running in main thread) to stop so we can write CSV
    request_shutdown()


def run_dashboard_server():
    """Entry point for multiprocessing child: construct Dash in child process."""
    # Silence child process stdout/stderr to prevent output from being printed
    import logging
    import os
    import sys

    # Redirect stdout and stderr to devnull
    try:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
    except Exception:
        pass

    # Also silence logging output
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("dash").setLevel(logging.ERROR)

    # Create and run the dashboard server
    server = SREGymDashboardServer(host="127.0.0.1", port=11451, debug=False)
    server.run(threaded=False)


def start_dashboard_process():
    """Start the dashboard server in a separate process and return the process object."""
    # Set multiprocessing start method to 'spawn' for better cross-platform compatibility
    try:
        multiprocessing.set_start_method("spawn", force=True)
    except RuntimeError:
        # Already set, ignore
        pass

    # Start dashboard in a separate process
    dashboard_process = None
    try:
        dashboard_process = multiprocessing.Process(
            target=run_dashboard_server,
            name="dashboard-server",
            daemon=True,  # Daemon process will be terminated when main process exits
        )
        dashboard_process.start()
        # Give dashboard a moment to start up
        time.sleep(2)
    except Exception as e:
        print(f"⚠️  Failed to start dashboard server: {e}", file=sys.stderr)

    return dashboard_process


def main():

    # set up the logger
    init_logger()

    # Start dashboard in a separate process
    dashboard_process = start_dashboard_process()

    conductor = Conductor()

    # Start the driver in the background; it will call request_shutdown() when finished
    driver_thread = threading.Thread(
        target=_run_driver_and_shutdown,
        args=(conductor,),
        name="driver",
        daemon=True,
    )
    driver_thread.start()

    # Start the MCP server in the background (lets the main thread run the Conductor API)
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
        # Give driver a moment to finish setting results
        driver_thread.join(timeout=5)

        # Terminate dashboard process gracefully if it's still running
        if dashboard_process is not None and dashboard_process.is_alive():
            try:
                # Send SIGTERM to allow graceful shutdown (triggers _export_on_exit)
                dashboard_process.terminate()
                # Give dashboard time to export trace data (export can take a few seconds)
                dashboard_process.join(timeout=5)
                # Force kill only if still alive after graceful shutdown timeout
                if dashboard_process.is_alive():
                    print("⚠️  Dashboard process did not exit gracefully, forcing termination...", file=sys.stderr)
                    dashboard_process.kill()
                    dashboard_process.join(timeout=1)
            except Exception as e:
                print(f"⚠️  Error terminating dashboard process: {e}", file=sys.stderr)

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
            print(f"✅ Benchmark complete! Results for {agent_name} written to {csv_path}")
    else:
        print("⚠️ No results to write.")

    sys.exit(0)


if __name__ == "__main__":
    main()
