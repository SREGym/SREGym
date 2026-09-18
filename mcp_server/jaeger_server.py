import logging
from datetime import datetime, timedelta

import anyio.to_thread
from fastmcp import FastMCP

from mcp_server.utils import ObservabilityClient

logger = logging.getLogger("all.mcp.jaeger_server")
logger.info("Starting Jaeger MCP Server")
mcp = FastMCP("Jaeger MCP Server")


def _get_services_sync() -> str:
    logger.debug("[ob_mcp] get_services called, getting jaeger services")

    jaeger_url = "http://jaeger-out.observe.svc.cluster.local:16686"
    jaeger_client = ObservabilityClient(jaeger_url)
    try:
        url = f"{jaeger_url}/api/services"
        response = jaeger_client.make_request("GET", url)
        logger.debug(f"[ob_mcp] get_services status code: {response.status_code}")
        logger.debug(f"[ob_mcp] get_services result: {response}")
        logger.debug(f"[ob_mcp] result: {response.json()}")
        services = str(response.json()["data"])
        result = services if services else "None"

        return result
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_services: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_services")
async def get_services() -> str:
    """Retrieve the list of service names from the Grafana instance.

    Returns:
        str: String of a list of service names available in Grafana or error information.
    """
    # ObservabilityClient.make_request is a blocking `requests` call (up to
    # REQUEST_TIMEOUT=120s, plus retries). FastMCP calls a sync tool function
    # inline on the single uvicorn worker, so running it directly here would
    # block every other request on this process, including the conductor's
    # port-forward health probe and every other session's calls. See the
    # matching comment on exec_kubectl_cmd_safely in mcp_server/kubectl_mcp_tools.py.
    return await anyio.to_thread.run_sync(_get_services_sync)


def _get_operations_sync(service: str) -> str:
    logger.debug("[ob_mcp] get_operations called, getting jaeger operations")

    jaeger_url = "http://jaeger-out.observe.svc.cluster.local:16686"
    jaeger_client = ObservabilityClient(jaeger_url)
    try:
        url = f"{jaeger_url}/api/operations"
        params = {"service": service}
        response = jaeger_client.make_request("GET", url, params=params)
        logger.debug(f"[ob_mcp] get_operations: {response.status_code}")
        operations = str(response.json()["data"])
        result = operations if operations else "None"

        return result
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_operations: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_operations")
async def get_operations(service: str) -> str:
    """Query available operations for a specific service from the Grafana instance.

    Args:
        service (str): The name of the service whose operations should be retrieved.

    Returns:
        str: String of a list of operation names associated with the specified service or error information.
    """
    return await anyio.to_thread.run_sync(_get_operations_sync, service)


def _get_traces_sync(service: str, last_n_minutes: int) -> str:
    logger.debug("[ob_mcp] get_traces called, getting jaeger traces")

    jaeger_url = "http://jaeger-out.observe.svc.cluster.local:16686"
    jaeger_client = ObservabilityClient(jaeger_url)
    try:
        url = f"{jaeger_url}/api/traces"
        start_time = datetime.now() - timedelta(minutes=last_n_minutes)
        start_time = int(start_time.timestamp() * 1_000_000)
        end_time = int(datetime.now().timestamp() * 1_000_000)
        logger.debug(f"[ob_mcp] get_traces start_time: {start_time}, end_time: {end_time}")
        params = {
            "service": service,
            "start": start_time,
            "end": end_time,
            "limit": 20,
        }
        response = jaeger_client.make_request("GET", url, params=params)
        logger.debug(f"[ob_mcp] get_traces: {response.status_code}")
        traces = str(response.json()["data"])
        result = traces if traces else "None"

        return result
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_traces: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_traces")
async def get_traces(service: str, last_n_minutes: int) -> str:
    """Get Jaeger traces for a given service in the last n minutes.

    Args:
        service (str): The name of the service for which to retrieve trace data.
        last_n_minutes (int): The time range (in minutes) to look back from the current time.

    Returns:
        str: String of Jaeger traces or error information
    """
    return await anyio.to_thread.run_sync(_get_traces_sync, service, last_n_minutes)


def _get_dependency_graph_sync(last_n_minutes: int) -> str:
    jaeger_url = "http://jaeger-out.observe.svc.cluster.local:16686"
    client = ObservabilityClient(jaeger_url)
    end_time = int(datetime.now().timestamp() * 1000)

    url = f"{jaeger_url}/api/dependencies"
    params = {"endTs": end_time, "lookback": last_n_minutes * 60 * 1000}

    response = client.make_request("GET", url, params=params)
    logger.info(f"[ob_mcp] get_dependency_graph: {response.status_code}")
    result = str(response.json())

    return result


@mcp.tool(name="get_dependency_graph")
async def get_dependency_graph(last_n_minutes: int = 30) -> str:
    """
    Get service dependency graph from Jaeger's native dependencies API.
    Args:
        last_n_minutes (int): The time range (in minutes) to look back from the current time.
    Returns:
        str: JSON object representing the dependency graph.
    """
    return await anyio.to_thread.run_sync(_get_dependency_graph_sync, last_n_minutes)
