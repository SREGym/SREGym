import logging
import os
import os.path
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Annotated, Optional, Union

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langchain_core.tools.base import ArgsSchema, BaseTool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from pydantic import BaseModel, Field

from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools
from clients.langgraph_agent.state import State
from clients.langgraph_agent.tools.text_editing.file_manip import update_file_vars_in_state
from clients.langgraph_agent.tools.text_editing.flake8_utils import flake8, format_flake8_output  # type: ignore
from clients.langgraph_agent.tools.text_editing.windowed_file import (  # type: ignore
    FileNotOpened,
    TextNotFound,
    WindowedFile,
)

USE_HTTP = True
USE_SUMMARIES = True
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@tool("get_metrics", description="Get metrics from Prometheus using a query")
async def get_metrics(
    query: str,
    state: Annotated[dict, InjectedState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Get metrics from Prometheus using a query."""
    logger.info(f"get_metrics called with query: {query}")
    logger.info("Calling MCP get_metrics from langchain get_metrics")
    exit_stack = AsyncExitStack()
    server_name = "prometheus"
    if USE_HTTP:
        logger.info("Using HTTP, connecting to server.")
        # server_url = "http://127.0.0.1:9953/sse"
        server_url = "http://127.0.0.1:8000/sse"
        # Register both the SSE client and session with an async exit stack so they will automatically clean up when you're done (e.g. close connections properly

        # opens the actual communication channel to the MCP server
        # Connect to the SSE stream
        # Wrap that connection in a ClientSession so you can call MCP tools
        # Automatically clean up everything when the async block finishes
        http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
        session = await exit_stack.enter_async_context(ClientSession(*http_transport))
    else:
        server_path = f"{os.getcwd()}/mcp_server/prometheus_server.py"
        logger.info(f"Connecting to server: {server_name} at path: {server_path}")
        is_python = server_path.endswith(".py")
        is_js = server_path.endswith(".js")
        if not (is_python or is_js):
            raise ValueError("Server path must be a Python or JavaScript file.")
        command = sys.executable if is_python else "node"
        server_parameters = StdioServerParameters(
            command=command,
            args=[server_path],
            server_name=server_name,
            is_python=is_python,
            is_js=is_js,
        )
        stdio_transport = await exit_stack.enter_async_context(stdio_client(server_parameters))
        stdio, write = stdio_transport
        session = await exit_stack.enter_async_context(ClientSession(*stdio_transport))
    await session.initialize()
    logger.info("Session created, calling get_metrics tool.")
    # Makes a request to the MCP server to get available tools
    response = await session.list_tools()
    # response.tools returns the actual list of tools
    tools = response.tools
    logger.info(f"Available tools: {tools}")
    if not tools:
        raise ValueError("No tools found in session.")
    result = await session.call_tool(
        "get_metrics",
        arguments={
            "query": query,
        },
    )
    logger.info(f"Result: {result}")
    metrics = result.content[0].text
    logger.info(f"Metrics received: {metrics}")
    await exit_stack.aclose()

    if USE_SUMMARIES:
        metrics = _summarize_metrics(result)
        logger.info(f"Summary: {metrics}")

    return Command(
        update=update_file_vars_in_state(state, ToolMessage(content=metrics, tool_call_id=tool_call_id)),
    )


def _summarize_metrics(metrics):
    logger.info("=== _summarize_metrics called ===")

    system_prompt = """
You are an expert Site Reliability Engineering tool. You are given raw microservice metrics as JSON dictionaries.

Your task:

1. Parse the raw metrics to identify potential root causes for incidents.
2. Summarize the metrics succinctly.
3. Provide raw metrics data as strings (do not explain them generically).
4. Use the following output format STRICTLY:

SERVICE NAME: <insert service name from metric>
SUMMARY:
<summary of metrics, possible root cause, and raw metrics as string>

Example:

SERVICE NAME: auth-service
SUMMARY:
High CPU usage detected (90%+), memory usage stable. Possible cause: infinite loop in request handler.

Raw metrics:
{"cpu_usage": "95", "memory_usage": "512MB"}

If you do not have enough data to determine root cause, state 'Insufficient data to determine root cause' and provide raw metrics.
"""

    logger.info(f"raw metrics received: {metrics}")
    llm = get_llm_backend_for_tools()
    # then use this `llm` for inference
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=metrics.content[0].text),
    ]

    metrics_summary = llm.inference(messages=messages)
    # metrics_summary = llm.inference(messages=metrics.content[0].text, system_prompt=system_prompt)
    logger.info(f"Metrics summary: {metrics_summary}")
    return metrics_summary
