import logging
import os

import uvicorn
from fastmcp.server.http import create_sse_app
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from mcp_server.configs.load_all_cfg import mcp_server_cfg
from mcp_server.jaeger_server import mcp as observability_mcp
from mcp_server.kubectl_mcp_tools import kubectl_mcp
from mcp_server.loki_server import mcp as loki_mcp
from mcp_server.prometheus_server import mcp as prometheus_mcp
from sregym.service.k8s_proxy import KubernetesAPIProxy

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

routes = [
    Mount("/kubectl", app=create_sse_app(kubectl_mcp, "/messages/", "/sse")),
    Mount("/jaeger", app=create_sse_app(observability_mcp, "/messages/", "/sse")),
    Mount("/loki", app=create_sse_app(loki_mcp, "/messages/", "/sse")),
    Mount("/prometheus", app=create_sse_app(prometheus_mcp, "/messages/", "/sse")),
]

# Submit proxy is only available when a conductor API endpoint is configured
# (e.g. by the Resolve agent via kubectl set env)
if os.getenv("API_HOSTNAME"):
    from mcp_server.submit_server import mcp as submit_mcp

    routes.append(Mount("/submit", app=create_sse_app(submit_mcp, "/messages/", "/sse")))
    logger.info("Submit proxy enabled (API_HOSTNAME=%s)", os.getenv("API_HOSTNAME"))

app = Starlette(
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
    routes=routes,
)

if __name__ == "__main__":
    # Start the Kubernetes API filtering proxy so kubectl commands executed by
    # MCP tools go through the proxy and have load-generator / chaos resources
    # filtered out.  The proxy picks up in-cluster ServiceAccount credentials
    # automatically and listens on a local port.
    proxy_port = 16443
    proxy = KubernetesAPIProxy(listen_port=proxy_port)
    proxy.start()
    kubeconfig_path = proxy.generate_agent_kubeconfig()
    os.environ["KUBECONFIG"] = kubeconfig_path
    logger.info(f"Kubernetes API proxy started on port {proxy_port}, KUBECONFIG={kubeconfig_path}")

    port = mcp_server_cfg.mcp_server_port
    host = "0.0.0.0" if mcp_server_cfg.expose_server else "127.0.0.1"
    logger.info("Starting SREGym MCP Server")
    uvicorn.run(app, host=host, port=port)
