import logging
from fastmcp import FastMCP
from utils import ObservabilityClient

logger = logging.getLogger("Prometheus MCP Server")
logger.info("Starting Prometheus MCP Server")

# Here, I initialize the FastMCP server with the name "Prometheus MCP Server
mcp = FastMCP("Prometheus MCP Server")


@mcp.tool(name="get_metrics")
def get_metrics(query: str) -> str:
    """Query real-time metrics data from the Prometheus instance.

        Args:
            query (str): A Prometheus Query Language (PromQL) expression used to fetch metric values.

        Returns:
            str: String of metric results, including timestamps, values, and labels or error information.
    """

    logger.info("[prom_mcp] get_metrics called, getting prometheus metrics")
    prometheus_url = "http://localhost:32000"
    observability_client = ObservabilityClient(prometheus_url)
    try:
        url = f"{prometheus_url}/api/v1/query"
        param = {"query": query}
        response = observability_client.make_request("GET", url, params=param)
        logger.info(f"[prom_mcp] get_metrics status code: {response.status_code}")
        logger.info(f"[prom_mcp] get_metrics result: {response}")
        metrics = str(response.json()["data"])
        return metrics if metrics else "None"
    except Exception as e:
        err_str = f"[prom_mcp] Error querying get_metrics: {str(e)}"
        logger.error(err_str)
        return err_str
