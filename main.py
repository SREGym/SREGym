import asyncio
import csv
import logging
import os
import sys
import threading
import time
from datetime import datetime

import uvicorn
from rich.console import Console

from dashboard.dashboard_app import SREGymDashboardServer
from dashboard.proxy import LogProxy
from mcp_server.configs.load_all_cfg import mcp_server_cfg
from mcp_server.sregym_mcp_server import app as mcp_app
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import get_agent
from sregym.conductor.conductor import Conductor
from sregym.conductor.conductor_api import request_shutdown, run_api

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

        all_results = []
        for pid in conductor.problems.get_problem_ids():
            console.log(f"\n🔍 Starting problem: {pid}")

            conductor.problem_id = pid

            await conductor.start_problem()
            agent_to_start = os.environ.get("SREGYM_AGENT", "stratus")
            reg = get_agent(agent_to_start)
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
            all_results.append(snapshot)

            fieldnames = sorted({key for row in all_results for key in row.keys()})
            current_date_time = get_current_datetime_formatted()
            csv_path = f"{current_date_time}_arena_{pid}_results.csv"
            with open(csv_path, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_results)
            print(f"✅ Problem {pid} complete! Results written to {csv_path}")

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
    # Silence child process stdout/stderr and noisy loggers
    import logging
    import os
    import sys

    try:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    server = SREGymDashboardServer(host="127.0.0.1", port=11451, debug=False)
    server.run(threaded=False)
    print("Dashboard server started on 127.0.0.1:11451")


def main():
    # set up the logger
    logging.getLogger("sregym-global").setLevel(logging.INFO)
    logging.getLogger("sregym-global").addHandler(LogProxy())

    """
    try:
        set_start_method("spawn")
    except RuntimeError:
        pass

    # Start dashboard in a separate process; construct server inside the child
    p = Process(target=run_dashboard_server, daemon=True)
    p.start()
    
    time.sleep(5)
    """

    # Get agent name from environment variable or default to "agent"
    agent_name = os.environ.get("SREGYM_AGENT", "agent")
    conductor = Conductor()
    conductor.register_agent(agent_name)

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

    # When API shuts down, collect results from driver
    results = getattr(main, "results", [])

    if results:
        fieldnames = sorted({key for row in results for key in row.keys()})
        current_date_time = get_current_datetime_formatted()
        csv_path = f"{current_date_time}_{agent_name}_ALL_results.csv"
        with open(csv_path, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print(f"✅ Benchmark complete! Results written to {csv_path}")
    else:
        print("⚠️ No results to write.")

    sys.exit(0)


if __name__ == "__main__":
    main()
