import logging
import os
import sys
from contextlib import AsyncExitStack
from typing import Optional

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools.base import ArgsSchema, BaseTool
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from pydantic import BaseModel, Field

from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools

USE_HTTP = True
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class GetMetricsInput(BaseModel):
    query: str = Field(description="Prometheus query to get metrics")


# FOR EVERY LANGRAPH TOOL U HAVE, MANDORY: NAME AND DECRIPTION
# We use this format because we run with HTTP and want to define a_run and _run
class GetMetrics(BaseTool):
    name: str = "get_metrics"
    description: str = "Get metrics from Prometheus using a query"
    args_schema: Optional[ArgsSchema] = GetMetricsInput

    def _summarize_metrics(self, metrics):
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

        logger.info(f"raw traces received: {metrics}")
        llm = get_llm_backend_for_tools()
        # then use this `llm` for inference
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=metrics.content[0].text),
        ]

        metrics_summary = llm.inference(messages=messages)
        logger.info(f"Traces summary: {metrics_summary}")
        return metrics_summary

    def _run(self, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        logger.error("No sync version of tools, exiting.")
        sys.exit(1)

    async def _arun(self, query, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
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
        summary = self._summarize_metrics(result)
        logger.info(f"Summary: {summary}")
        await exit_stack.aclose()
        return summary
