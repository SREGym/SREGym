"""Resolve AI infrastructure setup and teardown.

Manages the Resolve satellite Helm chart and MCP server submit proxy
configuration. The conductor API is reached directly via the host IP
(no ktunnel tunnel required).
"""

import logging
import socket
import subprocess

from sregym.service.helm import Helm

logger = logging.getLogger(__name__)

SATELLITE_RELEASE = "resolve-satellite"
SATELLITE_CHART = "oci://registry-1.docker.io/resolveaihq/satellite-chart"
SATELLITE_VALUES = "resolve-values.yaml"
SATELLITE_NAMESPACE = "default"
MCP_DEPLOYMENT = "deployment/mcp-server"
MCP_NAMESPACE = "sregym"
CONDUCTOR_PORT = "8000"


def _get_host_ip() -> str:
    """Return the primary IP of this host (reachable from cluster pods)."""
    result = subprocess.run(
        ["kubectl", "get", "node", socket.gethostname(), "-o", "jsonpath={.status.addresses[0].address}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    # Fallback: connect to a remote address to find the outgoing interface IP
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


class ResolveSetup:
    def __init__(self):
        self._host_ip: str | None = None

    def start(self):
        """Set up all Resolve-specific infrastructure."""
        self._host_ip = _get_host_ip()
        logger.info(f"Conductor API reachable at {self._host_ip}:{CONDUCTOR_PORT}")
        self._enable_submit_proxy()
        self._install_satellite()

    def stop(self):
        """Tear down all Resolve-specific infrastructure."""
        self._uninstall_satellite()
        self._disable_submit_proxy()

    def _enable_submit_proxy(self):
        """Patch the MCP server deployment to enable the submit proxy."""
        logger.info("Enabling submit proxy on MCP server...")
        result = subprocess.run(
            [
                "kubectl",
                "set",
                "env",
                MCP_DEPLOYMENT,
                "-n",
                MCP_NAMESPACE,
                f"API_HOSTNAME={self._host_ip}",
                f"API_PORT={CONDUCTOR_PORT}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to patch MCP server env: {result.stderr}")
        # Wait for rollout to complete
        logger.info("Waiting for MCP server rollout...")
        subprocess.run(
            ["kubectl", "rollout", "status", MCP_DEPLOYMENT, "-n", MCP_NAMESPACE, "--timeout=120s"],
            capture_output=True,
            text=True,
        )
        logger.info("Submit proxy enabled on MCP server")

    def _disable_submit_proxy(self):
        """Remove the submit proxy env vars from the MCP server deployment."""
        logger.info("Disabling submit proxy on MCP server...")
        result = subprocess.run(
            [
                "kubectl",
                "set",
                "env",
                MCP_DEPLOYMENT,
                "-n",
                MCP_NAMESPACE,
                "API_HOSTNAME-",
                "API_PORT-",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Failed to remove MCP server env vars: {result.stderr}")
        else:
            logger.info("Submit proxy disabled on MCP server")

    def _install_satellite(self):
        """Install the Resolve satellite Helm chart."""
        if Helm.exists_release(SATELLITE_RELEASE, SATELLITE_NAMESPACE):
            logger.info("Satellite already installed, upgrading...")
            Helm.upgrade(
                release_name=SATELLITE_RELEASE,
                chart_path=SATELLITE_CHART,
                namespace=SATELLITE_NAMESPACE,
                values_file=SATELLITE_VALUES,
            )
        else:
            Helm.install(
                release_name=SATELLITE_RELEASE,
                chart_path=SATELLITE_CHART,
                namespace=SATELLITE_NAMESPACE,
                remote_chart=True,
                extra_args=["-f", SATELLITE_VALUES],
            )

    def _uninstall_satellite(self):
        """Uninstall the Resolve satellite Helm chart."""
        try:
            Helm.uninstall(
                release_name=SATELLITE_RELEASE,
                namespace=SATELLITE_NAMESPACE,
            )
        except RuntimeError as e:
            logger.warning(f"Satellite uninstall error (non-fatal): {e}")
