import ast
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastmcp import Client
from fastmcp.client import SSETransport
from mcp import ClientSession
from mcp.client.sse import sse_client

from clients.harness.problem_id import resolve_problem_id
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


def parse_submit_command(command: str) -> tuple[str | None, str] | None:
    """Parse preferred staged and legacy one-argument demo submissions."""
    try:
        expression = ast.parse(command, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(expression, ast.Call):
        return None
    if not isinstance(expression.func, ast.Name) or expression.func.id != "submit":
        return None
    if expression.keywords:
        return None
    try:
        arguments = [ast.literal_eval(argument) for argument in expression.args]
    except (ValueError, TypeError):
        return None
    if len(arguments) == 1 and isinstance(arguments[0], str):
        return None, arguments[0]
    if (
        len(arguments) == 2
        and isinstance(arguments[0], str)
        and arguments[0] in {"diagnosis", "mitigation"}
        and isinstance(arguments[1], str)
    ):
        return arguments[0], arguments[1]
    return None


def resolve_demo_submission_stage(explicit_stage: str | None, previous_accepted_stage: str | None) -> str | None:
    """Resolve legacy demo calls without racing diagnosis evaluation."""
    if explicit_stage is not None:
        return explicit_stage
    if previous_accepted_stage == "diagnosis":
        return "mitigation"
    return None


async def manual_submit_tool(ans: str, *, stage: str | None = None) -> str:
    ltc = LanggraphToolConfig()
    logging.info(f"_manually_ submitting to benchmark, answer: {ans}")

    async with (
        sse_client(url=ltc.submit_mcp_url) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        arguments = {"ans": ans}
        if stage is not None:
            arguments["stage"] = stage
        result = await session.call_tool("submit", arguments=arguments)
        result = ast.literal_eval(result.content[0].text)
        if result.get("status") not in {"200", "done"}:
            stage_label = stage or "current-stage"
            raise RuntimeError(f"Benchmark rejected the {stage_label} submission: {result}")
        logger.info("Submission complete. No further action is needed.")
        return result.get("stage") or stage or "done"


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
    problem_id = resolve_problem_id()
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
    # A legacy demo file uses submit("answer") twice. After the first call is
    # accepted as diagnosis, explicitly target mitigation on the next call so
    # it can wait safely while diagnosis grading is still running.
    previous_accepted_stage: str | None = None

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
            # Prefer submit("diagnosis", "text") / submit("mitigation", ""),
            # but keep the legacy submit("text") form for old demo scripts.
            submission = parse_submit_command(cmd)

            if submission:
                stage, ans = submission
                requested_stage = resolve_demo_submission_stage(stage, previous_accepted_stage)
                stage_label = requested_stage or "current-stage"
                logger.info(f"[Turn {idx + 1}] Performing deterministic {stage_label} submission: {ans}")
                start_time = time.perf_counter()
                try:
                    accepted_stage = await manual_submit_tool(ans, stage=requested_stage)
                    previous_accepted_stage = accepted_stage
                    text_result = f"Submitted {stage_label} answer: {ans}"
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

    save_trajectory(events, problem_id)
    logger.info("Demo Agent run finished.")


if __name__ == "__main__":
    asyncio.run(run_demo_agent())
