import contextlib
import os

from fastmcp import Context, FastMCP

from clients.stratus.stratus_utils.get_logger import get_logger
from mcp_server.utils import ObservabilityClient
from sregym.generators.noise.manager import get_noise_manager

logger = get_logger()
logger.info("Starting Loki MCP Server")

mcp = FastMCP("Loki MCP Server")


@mcp.tool(name="get_logs")
def get_logs(query: str, last_n_minutes: int = 15, ctx: Context = None) -> str:
    """Query logs from Loki using LogQL.

    Args:
        query (str): A LogQL query expression (e.g., '{namespace="default"}' or '{app="nginx"} |= "error"').
        last_n_minutes (int): Number of minutes to look back for logs. Defaults to 15.

    Returns:
        str: Log entries matching the query, or error information.
    """
    logger.info(f"[loki_mcp] get_logs called with query: {query}")

    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    ssid = None
    with contextlib.suppress(BaseException):
        if ctx:
            ssid = ctx.request_context.request.headers.get("sregym_ssid")

    noise_manager.on_tool_call("loki", query, ssid)

    # Use default port 3100 if LOKI_PORT is not set
    loki_port = os.environ.get("LOKI_PORT", "3100")
    loki_url = f"http://localhost:{loki_port}"
    observability_client = ObservabilityClient(loki_url)

    try:
        import time

        end_time = int(time.time() * 1e9)  # nanoseconds
        start_time = end_time - (last_n_minutes * 60 * 1_000_000_000)

        url = f"{loki_url}/loki/api/v1/query_range"
        params = {
            "query": query,
            "start": start_time,
            "end": end_time,
            "limit": 100,
        }

        response = observability_client.make_request("GET", url, params=params)
        logger.info(f"[loki_mcp] get_logs status code: {response.status_code}")

        data = response.json()
        if data.get("status") != "success":
            return f"Query failed: {data.get('error', 'Unknown error')}"

        results = data.get("data", {}).get("result", [])
        if not results:
            return "No logs found matching the query."

        # Format log entries
        log_lines = []
        for stream in results:
            labels = stream.get("stream", {})
            label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
            for entry in stream.get("values", []):
                timestamp, log_line = entry
                # Convert nanosecond timestamp to readable format
                ts_seconds = int(timestamp) / 1e9
                ts_readable = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_seconds))
                log_lines.append(f"[{ts_readable}] [{label_str}] {log_line}")

        result = "\n".join(log_lines) if log_lines else "No log entries found."

        # Noise Injection Hook (Post-execution)
        result = noise_manager.on_tool_result("loki", query, result, ssid)

        return result
    except Exception as e:
        err_str = f"[loki_mcp] Error querying get_logs: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_labels")
def get_labels(ctx: Context = None) -> str:
    """Get all available label names from Loki.

    Returns:
        str: List of available label names for filtering logs.
    """
    logger.info("[loki_mcp] get_labels called")

    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    ssid = None
    with contextlib.suppress(BaseException):
        if ctx:
            ssid = ctx.request_context.request.headers.get("sregym_ssid")

    noise_manager.on_tool_call("loki", "get_labels", ssid)

    loki_port = os.environ.get("LOKI_PORT", "3100")
    loki_url = f"http://localhost:{loki_port}"
    observability_client = ObservabilityClient(loki_url)

    try:
        url = f"{loki_url}/loki/api/v1/labels"
        response = observability_client.make_request("GET", url)
        logger.info(f"[loki_mcp] get_labels status code: {response.status_code}")

        data = response.json()
        if data.get("status") != "success":
            return f"Query failed: {data.get('error', 'Unknown error')}"

        labels = data.get("data", [])
        result = "Available labels:\n" + "\n".join(f"  - {label}" for label in labels)

        # Noise Injection Hook (Post-execution)
        result = noise_manager.on_tool_result("loki", "get_labels", result, ssid)

        return result
    except Exception as e:
        err_str = f"[loki_mcp] Error querying get_labels: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_label_values")
def get_label_values(label: str, ctx: Context = None) -> str:
    """Get all values for a specific label from Loki.

    Args:
        label (str): The label name to get values for (e.g., 'namespace', 'app', 'pod').

    Returns:
        str: List of values for the specified label.
    """
    logger.info(f"[loki_mcp] get_label_values called for label: {label}")

    # Noise Injection Hook (Pre-execution)
    noise_manager = get_noise_manager()
    ssid = None
    with contextlib.suppress(BaseException):
        if ctx:
            ssid = ctx.request_context.request.headers.get("sregym_ssid")

    noise_manager.on_tool_call("loki", f"get_label_values:{label}", ssid)

    loki_port = os.environ.get("LOKI_PORT", "3100")
    loki_url = f"http://localhost:{loki_port}"
    observability_client = ObservabilityClient(loki_url)

    try:
        url = f"{loki_url}/loki/api/v1/label/{label}/values"
        response = observability_client.make_request("GET", url)
        logger.info(f"[loki_mcp] get_label_values status code: {response.status_code}")

        data = response.json()
        if data.get("status") != "success":
            return f"Query failed: {data.get('error', 'Unknown error')}"

        values = data.get("data", [])
        if not values:
            return f"No values found for label '{label}'."

        result = f"Values for label '{label}':\n" + "\n".join(f"  - {value}" for value in values)

        # Noise Injection Hook (Post-execution)
        result = noise_manager.on_tool_result("loki", f"get_label_values:{label}", result, ssid)

        return result
    except Exception as e:
        err_str = f"[loki_mcp] Error querying get_label_values: {str(e)}"
        logger.error(err_str)
        return err_str
