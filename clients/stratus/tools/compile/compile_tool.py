import logging
import os.path
import subprocess
from pathlib import Path
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


@tool("compile_postgresql_server", description="Compile PostgreSQL server code")
def compile_postgresql_server(
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Compile PostgreSQL server code."""
    logger = logging.getLogger(__name__)
    logger.info("Compiling PostgreSQL server code...")
    logger.info(f"State: {state}")

    workdir = Path(state.get("workdir", "")).resolve()
    logger.info(f"Work directory: {workdir}")

    if not workdir.exists():
        return f"Work directory {workdir} does not exist. Please set the workdir in the state."

    env = os.environ.copy()
    env["PATH"] = str(Path.home() / "pgsql/bin") + ":" + env["PATH"]
    homedir = str(Path.home())
    logger.info(f"Home directory: {homedir}")

    if not workdir.exists():
        return f"Work directory {workdir} does not exist. Please set the workdir in the state."

    cmds = [
        ["./configure", f"--prefix={workdir}/pgsql", "--without-icu"],
        ["make"],
        ["make", "install"],
        [f"{homedir}/pgsql/bin/initdb", "-D", f"{homedir}/pgsql/data2"],
        [f"{homedir}/pgsql/bin/pg_ctl", "-D", f"{homedir}/pgsql/data2", "-l", "logfile", "start"],
        [f"{homedir}/pgsql/bin/createdb", "test"],
        [f"{homedir}/pgsql/bin/psql", "-d", "test", "-c", "\\l"],
    ]

    output = ""
    for cmd in cmds:
        process = subprocess.run(cmd, cwd=workdir, capture_output=True, shell=False, text=True, env=env)
        cmd_str = " ".join(cmd)
        output += f"$ {cmd_str}\n{process.stdout}\n{process.stderr}\n"
        logger.info(f"Command: {cmd_str}")
        logger.info(f"Output: {process.stdout}")
    return ToolMessage(tool_call_id=tool_call_id, content=output)