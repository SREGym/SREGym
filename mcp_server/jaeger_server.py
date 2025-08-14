import logging
import os
from datetime import datetime, timedelta

from fastmcp import FastMCP

from clients.stratus.stratus_utils.get_logger import get_logger
from mcp_server.utils import ObservabilityClient

logger = get_logger()
logger.info("Starting Jaeger MCP Server")
mcp = FastMCP("Jaeger MCP Server")


@mcp.tool(name="get_services")
def get_services() -> str:
    """Retrieve the list of service names from the Grafana instance.

    Args:

    Returns:
        str: String of a list of service names available in Grafana or error information.
    """

    logger.info("[ob_mcp] get_services called, getting jaeger services")
    jaeger_url = os.environ.get("JAEGER_BASE_URL", None)
    if jaeger_url is None:
        err_msg = "JAEGER_BASE_URL environment variable is not set!"
        logger.error(err_msg)
        raise RuntimeError(err_msg)
    jaeger_client = ObservabilityClient(jaeger_url)
    try:
        url = f"{jaeger_url}/api/services"
        response = jaeger_client.make_request("GET", url)
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
    jaeger_url = os.environ.get("JAEGER_BASE_URL", None)
    if jaeger_url is None:
        err_msg = "JAEGER_BASE_URL environment variable is not set!"
        logger.error(err_msg)
        raise RuntimeError(err_msg)
    jaeger_client = ObservabilityClient(jaeger_url)
    try:
        url = f"{jaeger_url}/api/operations"
        params = {"service": service}
        response = jaeger_client.make_request("GET", url, params=params)
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
    jaeger_url = os.environ.get("JAEGER_BASE_URL", None)
    if jaeger_url is None:
        err_msg = "JAEGER_BASE_URL environment variable is not set!"
        logger.error(err_msg)
        raise RuntimeError(err_msg)
    jaeger_client = ObservabilityClient(jaeger_url)
    try:
        url = f"{jaeger_url}/api/traces"
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
        response = jaeger_client.make_request("GET", url, params=params)
        logger.info(f"[ob_mcp] get_traces: {response.status_code}")
        traces = str(response.json()["data"])
        return traces if traces else "None"
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_traces: {str(e)}"
        logger.error(err_str)
        return err_str
