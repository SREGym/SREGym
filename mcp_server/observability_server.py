import logging
from datetime import datetime, timedelta

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import create_sse_app
from prometheus_server import mcp as prometheus_mcp
from starlette.applications import Starlette
from starlette.routing import Mount
from utils import ObservabilityClient

logger = logging.getLogger("Observability MCP Server")
logger.info("Starting Observability MCP Server")
mcp = FastMCP("Observability MCP Server")

grafana_url = "http://localhost:16686"
observability_client = ObservabilityClient(grafana_url)


@mcp.tool(name="get_services")
def get_services() -> str:
    """Retrieve the list of service names from the Grafana instance.

        Args:

        Returns:
            str: String of a list of service names available in Grafana or error information.
    """

    logger.info("[ob_mcp] get_services called, getting jaeger services")
    try:
        url = f"{grafana_url}/api/services"
        response = observability_client.make_request("GET", url)
        logger.info(f"[ob_mcp] get_services status code: {response.status_code}")
        logger.info(f"[ob_mcp] get_services result: {response}")
        logger.info(f"[ob_mcp] result: {response.json()}")
        services = str(response.json()["data"])
        return services if services else "None"
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_services: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_operations")
def get_operations(service: str) -> str:
    """Query available operations for a specific service from the Grafana instance.

        Args:
            service (str): The name of the service whose operations should be retrieved.

        Returns:
            str: String of a list of operation names associated with the specified service or error information.
    """

    logger.info("[ob_mcp] get_operations called, getting jaeger operations")
    try:
        url = f"{grafana_url}/api/operations"
        params = {"service": service}
        response = observability_client.make_request("GET", url, params=params)
        logger.info(f"[ob_mcp] get_operations: {response.status_code}")
        operations = str(response.json()["data"])
        return operations if operations else "None"
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_operations: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_traces")
def get_traces(service: str, last_n_minutes: int) -> str:
    """Get Jaeger traces for a given service in the last n minutes.

        Args:
            service (str): The name of the service for which to retrieve trace data.
            last_n_minutes (int): The time range (in minutes) to look back from the current time.

        Returns:
            str: String of Jaeger traces or error information
    """

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
        traces = str(response.json()["data"])
        return traces if traces else "None"
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_traces: {str(e)}"
        logger.error(err_str)
        return err_str


if __name__ == "__main__":
    app = Starlette(
        routes=[
            Mount("/jaeger", app=create_sse_app(mcp, "/messages/", "/sse")),
            Mount("/prometheus", app=create_sse_app(prometheus_mcp, "/messages/", "/sse")),
        ]
    )

    uvicorn.run(app, host="127.0.0.1", port=8000)
