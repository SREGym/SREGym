import logging
from contextlib import AsyncExitStack
from typing import Annotated

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from mcp import ClientSession
from mcp.client.sse import sse_client

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools
from clients.stratus.tools.text_editing.flake8_utils import flake8, format_flake8_output  # type: ignore
from clients.stratus.tools.text_editing.windowed_file import (  # type: ignore
    FileNotOpened,
    TextNotFound,
    WindowedFile,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

langgraph_tool_config = LanggraphToolConfig()

get_metrics_docstring = """
Query real-time metrics data from the Prometheus instance.

    Args:
        query (str): A Prometheus Query Language (PromQL) expression used to fetch metric values.

    Returns:
        dict: The raw Prometheus response containing metric results, including timestamps, values, and labels.
"""


@tool(description=get_metrics_docstring)
async def get_metrics(
    query: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:

    logger.info(f"get_metrics called with query: {query}")
    logger.info("Calling MCP get_metrics from langchain get_metrics")
    exit_stack = AsyncExitStack()
    logger.info("Using HTTP, connecting to server.")
    # server_url = "http://127.0.0.1:9953/sse"
    server_url = langgraph_tool_config.prometheus_mcp_url
    # Register both the SSE client and session with an async exit stack so they will automatically clean up when
    # you're done (e.g. close connections properly

    # opens the actual communication channel to the MCP server
    # Connect to the SSE stream
    # Wrap that connection in a ClientSession so you can call MCP tools
    # Automatically clean up everything when the async block finishes
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool(
        "get_metrics",
        arguments={
            "query": query,
        },
    )
    logger.info(f"Result: {result}")
    # metrics = result.content[0].text
    logger.info(f"Metrics received: {result}")
    await exit_stack.aclose()

    # if langgraph_tool_config.use_summaries and len(metrics) >= langgraph_tool_config.min_len_to_sum:
    #     metrics = _summarize_metrics(result)
    #     # logger.info(f"Summary: {metrics}")

    return Command(
        update={
            "messages": [
                ToolMessage(content=result, tool_call_id=tool_call_id),
            ]
        }
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

    # logger.info(f"raw metrics received: {metrics}")
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
