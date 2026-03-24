from fastmcp import FastMCP

from clients.stratus.stratus_utils.get_logger import get_logger
from mcp_server.utils import ObservabilityClient

logger = get_logger()
logger.info("Starting Prometheus MCP Server")

mcp = FastMCP("Prometheus MCP Server")


PROMETHEUS_URL = "http://prometheus-server.observe.svc.cluster.local:80"


@mcp.tool(name="get_metrics")
def get_metrics(query: str) -> str:
    """Query real-time metrics data from the Prometheus instance.

    Args:
        query (str): A Prometheus Query Language (PromQL) expression used to fetch metric values.

    Returns:
        str: String of metric results, including timestamps, values, and labels or error information.
    """

    logger.info("[prom_mcp] get_metrics called, getting prometheus metrics")

    observability_client = ObservabilityClient(PROMETHEUS_URL)
    try:
        url = f"{PROMETHEUS_URL}/api/v1/query"
        param = {"query": query}
        response = observability_client.make_request("GET", url, params=param)
        logger.info(f"[prom_mcp] get_metrics status code: {response.status_code}")
        logger.info(f"[prom_mcp] get_metrics result: {response}")
        metrics = str(response.json()["data"])
        result = metrics if metrics else "None"

        return result
    except Exception as e:
        err_str = f"[prom_mcp] Error querying get_metrics: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_alerts")
def get_alerts() -> str:
    """Get all currently firing alerts from the Prometheus instance.

    Returns a list of firing alerts with their labels (alertname, severity,
    namespace, service_name, etc.) and annotations (summary, description).

    Returns:
        str: String of firing alert data including alert names, labels,
             and annotations, or "No firing alerts" if none are active.
    """

    logger.info("[prom_mcp] get_alerts called")

    observability_client = ObservabilityClient(PROMETHEUS_URL)
    try:
        url = f"{PROMETHEUS_URL}/api/v1/alerts"
        response = observability_client.make_request("GET", url)
        logger.info(f"[prom_mcp] get_alerts status code: {response.status_code}")

        alerts = response.json().get("data", {}).get("alerts", [])
        firing = [a for a in alerts if a.get("state") == "firing"]

        if not firing:
            return "No firing alerts"

        return str(firing)
    except Exception as e:
        err_str = f"[prom_mcp] Error querying get_alerts: {str(e)}"
        logger.error(err_str)
        return err_str
