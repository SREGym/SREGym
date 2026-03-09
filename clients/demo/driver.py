import asyncio
import json
import logging
import os
import sys
import time
import uuid
from ast import literal_eval
from datetime import datetime
from pathlib import Path

import requests
from fastmcp import Client
from fastmcp.client import SSETransport
from mcp import ClientSession
from mcp.client.sse import sse_client

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from logger import init_logger

# for external call
sregym_core_path = Path(__file__).resolve().parents[2]
if str(sregym_core_path) not in sys.path:
    sys.path.insert(0, str(sregym_core_path))

init_logger()
logger = logging.getLogger("all.demo.driver")
logger.propagate = True
logger.setLevel(logging.DEBUG)

# File trigger paths
NEXT_FILE = Path("/tmp/next")
SKIP_FILE = Path("/tmp/skip")
QUIT_FILE = Path("/tmp/quit")


async def wait_for_file_trigger():
    """Wait for one of the trigger files to be created, then delete it and return the action."""
    while True:
        if QUIT_FILE.exists():
            QUIT_FILE.unlink()
            return "quit"
        if SKIP_FILE.exists():
            SKIP_FILE.unlink()
            return "skip"
        if NEXT_FILE.exists():
            NEXT_FILE.unlink()
            return "next"
        await asyncio.sleep(0.5)


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m%d_%H%M")
    return formatted_datetime


def get_curr_problem():
    ltc = LanggraphToolConfig()
    url = ltc.benchmark_current_problem
    try:
        response = requests.get(url)
        problem_str = str(response.text)
        problem = literal_eval(problem_str)
        return problem["problem_id"]
    except Exception as e:
        logger.error(f"[get_curr_problem] HTTP request failed: {e}")
        return "error"


async def manual_submit_tool(ans: str) -> str:
    ltc = LanggraphToolConfig()
    logging.info(f"_manually_ submitting to benchmark, answer: {ans}")

    async with (
        sse_client(url=ltc.submit_mcp_url) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        await session.call_tool(
            "submit",
            arguments={
                "ans": ans,
            },
        )
        logger.info("Submission complete. No further action is needed.")
        return "Submitted"


def save_trajectory(events, problem_id, output_dir=None):
    if output_dir is None:
        agent_logs_dir = os.environ.get("AGENT_LOGS_DIR")
        output_dir = os.path.join(agent_logs_dir, "trajectory") if agent_logs_dir else "."
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = get_current_datetime_formatted()
    trajectory_file = output_dir / f"{timestamp}_{problem_id}_demo_agent_trajectory.jsonl"

    with open(trajectory_file, "w", encoding="utf-8") as f:
        metadata = {
            "type": "metadata",
            "problem_id": problem_id,
            "timestamp": timestamp,
            "timestamp_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_events": len(events),
        }
        f.write(json.dumps(metadata) + "\n")

        for _idx, event in enumerate(events):
            f.write(json.dumps(event) + "\n")

    logger.info(f"[Driver] Saved trajectory to {trajectory_file}")
    return trajectory_file


async def run_demo_agent():
    problem_id = get_curr_problem()
    logger.info(f"Starting Demo Agent for problem: {problem_id}")

    cmds_file = Path(__file__).parent / Path("kubectl_cmds.txt")
    if not cmds_file.exists():
        logger.error("kubectl_cmds.txt not found! Please create it with kubectl commands, one per line.")
        return

    with open(cmds_file) as f:
        cmds = [line.strip() for line in f if line.strip()]

    if not cmds:
        logger.warning("kubectl_cmds.txt is empty. Nothing to execute.")
        return

    ltc = LanggraphToolConfig()
    session_id = str(uuid.uuid4())
    sse_timeout = float(os.getenv("SSE_READ_TIMEOUT", "3600"))
    if sse_timeout < 0:
        sse_timeout = None

    transport = SSETransport(
        url=ltc.kubectl_mcp_url,
        headers={"sregym_ssid": session_id},
        sse_read_timeout=sse_timeout,
    )

    events = []

    # Ensure any stale trigger files are removed
    for f in [NEXT_FILE, SKIP_FILE, QUIT_FILE]:
        if f.exists():
            f.unlink()

    print(f"\n{'*' * 50}", flush=True)
    print("DEMO AGENT ACTIVE (FILE-TRIGGER MODE)", flush=True)
    print(f"Advance commands: docker exec <id> touch {NEXT_FILE}", flush=True)
    print(f"Skip commands:    docker exec <id> touch {SKIP_FILE}", flush=True)
    print(f"Quit agent:       docker exec <id> touch {QUIT_FILE}", flush=True)
    print(f"{'*' * 50}\n", flush=True)

    async with Client(transport) as client:
        for idx, cmd in enumerate(cmds):
            print(f"\n{'=' * 20}", flush=True)
            print(f"WAITING FOR TRIGGER for command [{idx + 1}/{len(cmds)}]: {cmd}", flush=True)

            # Wait for file trigger
            action = await wait_for_file_trigger()

            if action == "quit":
                logger.info("User requested to quit via file trigger. Stopping execution.")
                break
            elif action == "skip":
                logger.info(f"Skipping command via file trigger: {cmd}")
                continue

            # Check if the command is a deterministic submission
            # Matches: submit("text") or submit('text')
            import re

            submit_match = re.match(r"^submit\([\"\'](.*)[\"\']\)$", cmd)

            if submit_match:
                ans = submit_match.group(1)
                logger.info(f"[Turn {idx + 1}] Performing deterministic submission: {ans}")
                start_time = time.perf_counter()
                try:
                    await manual_submit_tool(ans)
                    text_result = f"Submitted answer: {ans}"
                    status = "success"
                except Exception as e:
                    logger.error(f"[Turn {idx + 1}] Submission failed: {e}")
                    text_result = str(e)
                    status = "error"
            else:
                logger.info(f"[Turn {idx + 1}] Executing: {cmd}")
                start_time = time.perf_counter()
                try:
                    result = await client.call_tool("exec_kubectl_cmd_safely", arguments={"cmd": cmd})
                    text_result = "\n".join([part.text for part in result])
                    logger.info(f"[Turn {idx + 1}] Result: {text_result}")
                    status = "success"
                except Exception as e:
                    logger.error(f"[Turn {idx + 1}] Failed to execute: {e}")
                    text_result = str(e)
                    status = "error"

            execution_time = time.perf_counter() - start_time

            # Mock messages for compatibility with stratus visualization
            messages = [
                {"type": "HumanMessage", "content": f"Please execute the following command: {cmd}"},
                {
                    "type": "AIMessage",
                    "content": f"I will now execute: {cmd}",
                    "tool_calls": [{"name": "exec_kubectl_cmd_safely", "args": {"cmd": cmd}, "id": f"call_{idx}"}],
                },
                {"type": "ToolMessage", "content": text_result, "tool_call_id": f"call_{idx}"},
            ]

            event = {
                "type": "event",
                "event_index": idx,
                "stage": "demo",
                "num_steps": idx,
                "submitted": False,
                "messages": messages,
                "last_message": messages[-1],
                "command": cmd,
                "result": text_result,
                "status": status,
                "execution_time": execution_time,
                "timestamp": datetime.now().isoformat(),
            }
            events.append(event)

            await asyncio.sleep(0.5)

    # Manual submission at the end
    logger.info("All commands executed. Submitting results.")
    try:
        await manual_submit_tool("Demo agent execution completed.")
    except Exception as e:
        logger.error(f"Failed to submit: {e}")

    save_trajectory(events, problem_id)
    logger.info("Demo Agent run finished.")


if __name__ == "__main__":
    asyncio.run(run_demo_agent())
