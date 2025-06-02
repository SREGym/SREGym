import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base
from pydantic import AnyUrl
from utils import ObservabilityClient

logger = logging.getLogger("Observability MCP Server")
logger.info("Starting Observability MCP Server")
mcp = FastMCP("Observability MCP Server")


grafana_url = "http://localhost:16686"
observability_client = ObservabilityClient(grafana_url)


@mcp.tool(name="get_services")
def get_services():
    logger.info("[ob_mcp] get_services called, getting jaeger services")
    try:
        url = f"{grafana_url}/api/services"
        response = observability_client.make_request("GET", url)
        logger.info(f"[ob_mcp] get_services status code: {response.status_code}")
        logger.info(f"[ob_mcp] get_services result: {response}")
        return response.json()["data"]
    except Exception as e:
        logger.error(f"[ob_mcp] Error querying get_services: {str(e)}")
        return None


@mcp.tool(name="get_operations")
def get_operations(service: str):
    logger.info("[ob_mcp] get_operations called, getting jaeger operations")
    try:
        url = f"{grafana_url}/api/operations"
        params = {"service": service}
        response = observability_client.make_request("GET", url, params=params)
        logger.info(f"[ob_mcp] get_operations: {response.status_code}")
        return response.json()["data"]
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
    except Exception as e:
        logger.error(f"[ob_mcp] Error querying get_traces: {str(e)}")
        return None


if __name__ == "__main__":
    mcp.run()
