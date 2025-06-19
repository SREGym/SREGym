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

logger = logging.getLogger("Observability MCP Server")
logger.info("Starting Observability MCP Server")
mcp = FastMCP("Observability MCP Server")


grafana_url = "http://localhost:16686"
observability_client = ObservabilityClient(grafana_url)

USE_HTTP = True


@mcp.tool(name="get_services")
def get_services():
    logger.info("[ob_mcp] get_services called, getting jaeger services")
    try:
        url = f"{grafana_url}/api/services"
        response = observability_client.make_request("GET", url)
        logger.info(f"[ob_mcp] get_services status code: {response.status_code}")
        logger.info(f"[ob_mcp] get_services result: {response}")
        logger.info(f"[ob_mcp] result: {response.json()}")
        # FIXME: if response.json()["data"] is empty, forge an empty response
        return response.json()["data"]
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_services: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_operations")
def get_operations(service: str):
    logger.info("[ob_mcp] get_operations called, getting jaeger operations")
    try:
        url = f"{grafana_url}/api/operations"
        params = {"service": service}
        response = observability_client.make_request("GET", url, params=params)
        logger.info(f"[ob_mcp] get_operations: {response.status_code}")
        return response.json()["data"]
        # FIXME: if response.json()["data"] is empty, forge an empty response
    except Exception as e:
        logger.error(f"[ob_mcp] Error querying get_operations: {str(e)}")
        return None


@mcp.tool(name="get_traces")
def get_traces(service: str, last_n_minutes: int):
    logger.info("[ob_mcp] get_traces called, getting jaeger traces")
    try:
        url = f"{grafana_url}/api/traces"
        start_time = datetime.now() - timedelta(minutes=last_n_minutes)
        start_time = int(start_time.timestamp() * 1_000_000)
        end_time = int(datetime.now().timestamp() * 1_000_000)
        logger.info(f"[ob_mcp] get_traces start_time: {start_time}, end_time: {end_time}")
        params = {
            "service": service,
            "start": start_time,
            "end": end_time,
            "limit": 20,
        }
        response = observability_client.make_request("GET", url, params=params)
        logger.info(f"[ob_mcp] get_traces: {response.status_code}")
        return response.json()["data"]
        # FIXME: if response.json()["data"] is empty, forge an empty response
    except Exception as e:
        logger.error(f"[ob_mcp] Error querying get_traces: {str(e)}")
        return None


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
