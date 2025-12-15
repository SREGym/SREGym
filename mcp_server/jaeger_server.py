import logging
import os
from datetime import datetime, timedelta

from fastmcp import FastMCP, Context

from mcp_server.utils import ObservabilityClient
from sregym.generators.noise.manager import get_noise_manager

logger = logging.getLogger("all.mcp.jaeger_server")
logger.info("Starting Jaeger MCP Server")
mcp = FastMCP("Jaeger MCP Server")


@mcp.tool(name="get_services")
def get_services(ctx: Context) -> str:
    """Retrieve the list of service names from the Grafana instance.

    Args:
        ctx: Context object.

    Returns:
        str: String of a list of service names available in Grafana or error information.
    """

    logger.debug("[ob_mcp] get_services called, getting jaeger services")
    
    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    ssid = None
    try:
        ssid = ctx.request_context.request.headers.get("sregym_ssid")
    except:
        pass
    noise_manager.on_tool_call("jaeger", "get_services", ssid)

    jaeger_url = os.environ.get("JAEGER_BASE_URL", None)
    if jaeger_url is None:
        err_msg = "JAEGER_BASE_URL environment variable is not set!"
        logger.error(err_msg)
        raise RuntimeError(err_msg)
    jaeger_client = ObservabilityClient(jaeger_url)
    try:
        url = f"{jaeger_url}/api/services"
        response = jaeger_client.make_request("GET", url)
        logger.debug(f"[ob_mcp] get_services status code: {response.status_code}")
        logger.debug(f"[ob_mcp] get_services result: {response}")
        logger.debug(f"[ob_mcp] result: {response.json()}")
        services = str(response.json()["data"])
        result = services if services else "None"
        
        # Noise Injection Hook (Post-execution)
        result = noise_manager.on_tool_result("jaeger", "get_services", result, ssid)
        
        return result
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_services: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_operations")
def get_operations(service: str, ctx: Context) -> str:
    """Query available operations for a specific service from the Grafana instance.

    Args:
        service (str): The name of the service whose operations should be retrieved.
        ctx: Context object.

    Returns:
        str: String of a list of operation names associated with the specified service or error information.
    """

    logger.debug("[ob_mcp] get_operations called, getting jaeger operations")
    
    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    ssid = None
    try:
        ssid = ctx.request_context.request.headers.get("sregym_ssid")
    except:
        pass
    noise_manager.on_tool_call("jaeger", f"get_operations {service}", ssid)

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
        logger.debug(f"[ob_mcp] get_operations: {response.status_code}")
        operations = str(response.json()["data"])
        result = operations if operations else "None"
        
        # Noise Injection Hook (Post-execution)
        result = noise_manager.on_tool_result("jaeger", f"get_operations {service}", result, ssid)
        
        return result
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_operations: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_traces")
def get_traces(service: str, last_n_minutes: int, ctx: Context) -> str:
    """Get Jaeger traces for a given service in the last n minutes.

    Args:
        service (str): The name of the service for which to retrieve trace data.
        last_n_minutes (int): The time range (in minutes) to look back from the current time.
        ctx: Context object.

    Returns:
        str: String of Jaeger traces or error information
    """

    logger.debug("[ob_mcp] get_traces called, getting jaeger traces")
    
    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    ssid = None
    try:
        ssid = ctx.request_context.request.headers.get("sregym_ssid")
    except:
        pass
    noise_manager.on_tool_call("jaeger", f"get_traces {service}", ssid)

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
        
        # Noise Injection Hook (Post-execution)
        result = noise_manager.on_tool_result("jaeger", f"get_traces {service}", result, ssid)
        
        return result
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_traces: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_dependency_graph")
def get_dependency_graph(ctx: Context, last_n_minutes: int = 30) -> str:
    """
    Get service dependency graph from Jaeger's native dependencies API.
    Args:
        ctx: Context object.
        last_n_minutes (int): The time range (in minutes) to look back from the current time.
    Returns:
        str: JSON object representing the dependency graph.
    """
    
    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    ssid = None
    try:
        ssid = ctx.request_context.request.headers.get("sregym_ssid")
    except:
        pass
    noise_manager.on_tool_call("jaeger", "get_dependency_graph", ssid)

    jaeger_url = os.environ.get("JAEGER_BASE_URL")
    if not jaeger_url:
        raise RuntimeError("JAEGER_BASE_URL environment variable is not set!")

    client = ObservabilityClient(jaeger_url)
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(minutes=last_n_minutes)).timestamp() * 1000)

    url = f"{jaeger_url}/api/dependencies"
    params = {"endTs": end_time, "lookback": last_n_minutes * 60 * 1000}

    response = client.make_request("GET", url, params=params)
    logger.info(f"[ob_mcp] get_dependency_graph: {response.status_code}")
    result = str(response.json())
    
    # Noise Injection Hook (Post-execution)
    result = noise_manager.on_tool_result("jaeger", "get_dependency_graph", result, ssid)
    
    return result
