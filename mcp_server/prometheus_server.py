import logging
import os

from fastmcp import FastMCP, Context

from clients.stratus.stratus_utils.get_logger import get_logger
from mcp_server.utils import ObservabilityClient
from sregym.generators.noise.manager import get_noise_manager

logger = get_logger()
logger.info("Starting Prometheus MCP Server")

mcp = FastMCP("Prometheus MCP Server")


@mcp.tool(name="get_metrics")
def get_metrics(query: str, ctx: Context) -> str:
    """Query real-time metrics data from the Prometheus instance.

    Args:
        query (str): A Prometheus Query Language (PromQL) expression used to fetch metric values.
        ctx: Context object.

    Returns:
        str: String of metric results, including timestamps, values, and labels or error information.
    """

    logger.info("[prom_mcp] get_metrics called, getting prometheus metrics")
    
    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    # Try to get session ID if possible, otherwise None
    ssid = None
    try:
        ssid = ctx.request_context.request.headers.get("sregym_ssid")
    except:
        pass
        
    noise_manager.on_tool_call("prometheus", query, ssid)

    prometheus_port = os.environ.get("PROMETHEUS_PORT", None)
    if prometheus_port is None:
        err_msg = "PROMETHEUS_PORT environment variable is not set!"
        logger.error(err_msg)
        raise RuntimeError(err_msg)
    prometheus_url = "http://localhost:" + os.environ["PROMETHEUS_PORT"]
    observability_client = ObservabilityClient(prometheus_url)
    try:
        url = f"{prometheus_url}/api/v1/query"
        param = {"query": query}
        response = observability_client.make_request("GET", url, params=param)
        logger.info(f"[prom_mcp] get_metrics status code: {response.status_code}")
        logger.info(f"[prom_mcp] get_metrics result: {response}")
        metrics = str(response.json()["data"])
        result = metrics if metrics else "None"
        
        # Noise Injection Hook (Post-execution)
        result = noise_manager.on_tool_result("prometheus", query, result, ssid)
        
        return result
    except Exception as e:
        err_str = f"[prom_mcp] Error querying get_metrics: {str(e)}"
        logger.error(err_str)
        return err_str
