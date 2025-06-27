import argparse
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Coroutine

import httpx
import mcp.types as types
import uvicorn
from mcp.server import Server
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base
from mcp.server.sse import SseServerTransport
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from utils import ObservabilityClient

logger = logging.getLogger("Prometheus MCP Server")
logger.info("Starting Prometheus MCP Server")

#Here, I initialize the FastMCP server with the name "Prometheus MCP Server
mcp = FastMCP("Prometheus MCP Server")


USE_HTTP = True

@mcp.tool(name="get_metrics")
def get_metrics(query: str):
    logger.info("[prom_mcp] get_metrics called, getting prometheus metrics")
    prometheus_url = "http://localhost:32000"
    observability_client = ObservabilityClient(prometheus_url)
    try:
        url = f"{prometheus_url}/api/v1/query"
        param= {"query": query }
        response = observability_client.make_request("GET", url, params=param)
        logger.info(f"[prom_mcp] get_metrics status code: {response.status_code}")
        logger.info(f"[prom_mcp] get_metrics result: {response}")
        return response.json()["data"]["result"]
    except Exception as e:
        err_str = f"[prom_mcp] Error querying get_metrics: {str(e)}"
        logger.error(err_str)
        return err_str

def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    """Create a Starlette application that can server the provied mcp server with SSE."""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


if __name__ == "__main__":
    if USE_HTTP:
        mcp.run(transport="sse")
    else:
        mcp.run()
