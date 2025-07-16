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

USE_HTTP = True


@mcp.tool(name="get_services")
def get_services():
    logger.info("[ob_mcp] get_services called, getting jaeger services")
    try:
        url = f"{grafana_url}/api/services"
        response = observability_client.make_request("GET", url)
        logger.info(f"[ob_mcp] get_services status code: {response.status_code}")
        logger.info(f"[ob_mcp] get_services result: {response}")
        logger.info(f"[ob_mcp] result: {response.json()}")
        services = response.json()["data"]
        assert type(services) == list, f"The type of the returned result should be list but get {type(services)}."
        if len(services) > 0:
            return services
        else:
            return "The result of your query is empty. Please recheck the parameters you use."
    except Exception as e:
        err_str = f"[ob_mcp] Error querying get_services: {str(e)}"
        logger.error(err_str)
        return err_str


@mcp.tool(name="get_operations")
def get_operations(service: str):
    logger.info("[ob_mcp] get_operations called, getting jaeger operations")
    try:
        url = f"{grafana_url}/api/operations"
        params = {"service": service}
        response = observability_client.make_request("GET", url, params=params)
        logger.info(f"[ob_mcp] get_operations: {response.status_code}")
        operations = response.json()["data"]
        assert type(operations) == list, f"The type of the returned result should be list but get {type(operations)}."
        if len(operations) > 0:
            return operations
        else:
            return "The result of your query is empty. Please recheck the parameters you use."
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
        traces = response.json()["data"]
        assert type(traces) == list, f"The type of the returned result should be list but get {type(traces)}."
        if len(traces) > 0:
            return traces
        else:
            return "The result of your query is empty. Please recheck the parameters you use."
    except Exception as e:
        logger.error(f"[ob_mcp] Error querying get_traces: {str(e)}")
        return None


if __name__ == "__main__":
    app = Starlette(
        routes=[
            Mount("/jaeger", app=create_sse_app(mcp, "/messages/", "/sse")),
            Mount("/prometheus", app=create_sse_app(prometheus_mcp, "/messages/", "/sse")),
        ]
    )

    uvicorn.run(app, host="127.0.0.1", port=8000)
