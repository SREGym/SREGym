import logging

import uvicorn
from fastmcp.server.http import create_sse_app
from starlette.applications import Starlette
from starlette.routing import Mount

from mcp_server.configs.load_all_cfg import mcp_server_cfg
from mcp_server.jaeger_server import mcp as observability_mcp
from mcp_server.kubectl_mcp_tools import kubectl_mcp
from mcp_server.prometheus_server import mcp as prometheus_mcp
from mcp_server.submit_server import mcp as submit_mcp

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Starlette(
    routes=[
        Mount("/kubectl_mcp_tools", app=create_sse_app(kubectl_mcp, "/messages/", "/sse")),
        Mount("/jaeger", app=create_sse_app(observability_mcp, "/messages/", "/sse")),
        Mount("/prometheus", app=create_sse_app(prometheus_mcp, "/messages/", "/sse")),
        Mount("/submit", app=create_sse_app(submit_mcp, "/messages/", "/sse")),
    ]
)

if __name__ == "__main__":
    port = mcp_server_cfg.mcp_server_port
    host = "0.0.0.0" if mcp_server_cfg.expose_server else "127.0.0.1"
    logger.info("Starting SREGym MCP Server")
    uvicorn.run(app, host=host, port=port)
