import logging
from fastmcp import FastMCP
from utils import ObservabilityClient

logger = logging.getLogger("Prometheus MCP Server")
logger.info("Starting Prometheus MCP Server")

# Here, I initialize the FastMCP server with the name "Prometheus MCP Server
mcp = FastMCP("Prometheus MCP Server")

USE_HTTP = True


@mcp.tool(name="get_metrics")
def get_metrics(query: str):
    logger.info("[prom_mcp] get_metrics called, getting prometheus metrics")
    prometheus_url = "http://localhost:32000"
    observability_client = ObservabilityClient(prometheus_url)
    try:
        url = f"{prometheus_url}/api/v1/query"
        param = {"query": query}
        response = observability_client.make_request("GET", url, params=param)
        logger.info(f"[prom_mcp] get_metrics status code: {response.status_code}")
        logger.info(f"[prom_mcp] get_metrics result: {response}")
        metrics = response.json()["data"]
        assert type(metrics) == dict, f"The type of the returned result should be list but get {type(metrics)}."
        if len(metrics) > 0:
            return metrics
        else:
            return "The result of your query is empty. Please recheck the parameters you use."
    except Exception as e:
        err_str = f"[prom_mcp] Error querying get_metrics: {str(e)}"
        logger.error(err_str)
        return err_str


if __name__ == "__main__":
    if USE_HTTP:
        mcp.run(transport="sse")
    else:
        mcp.run()
