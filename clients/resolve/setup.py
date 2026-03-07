"""Resolve AI infrastructure setup and teardown.

Manages the ktunnel reverse tunnel, Resolve satellite Helm chart,
and MCP server submit proxy configuration.
"""

import logging
import subprocess
import time

from sregym.service.helm import Helm

logger = logging.getLogger(__name__)

SATELLITE_RELEASE = "resolve-satellite"
SATELLITE_CHART = "oci://registry-1.docker.io/resolveaihq/satellite-chart"
SATELLITE_VALUES = "resolve-values.yaml"
SATELLITE_NAMESPACE = "default"
KTUNNEL_NAMESPACE = "sregym"
KTUNNEL_SERVICE = "conductor-api"
KTUNNEL_PORT = "8000:8000"
MCP_DEPLOYMENT = "deployment/mcp-server"
MCP_NAMESPACE = "sregym"


class ResolveSetup:
    def __init__(self):
        self._ktunnel_proc: subprocess.Popen | None = None

    def start(self):
        """Set up all Resolve-specific infrastructure."""
        self._start_ktunnel()
        self._enable_submit_proxy()
        self._install_satellite()

    def stop(self):
        """Tear down all Resolve-specific infrastructure."""
        self._uninstall_satellite()
        self._disable_submit_proxy()
        self._stop_ktunnel()

    def _clean_stale_ktunnel(self):
        """Remove leftover ktunnel resources from a previous run."""
        subprocess.run(
            ["kubectl", "delete", "deployment", KTUNNEL_SERVICE, "-n", KTUNNEL_NAMESPACE, "--ignore-not-found"],
            capture_output=True,
        )
        subprocess.run(
            ["kubectl", "delete", "service", KTUNNEL_SERVICE, "-n", KTUNNEL_NAMESPACE, "--ignore-not-found"],
            capture_output=True,
        )

    def _start_ktunnel(self):
        """Start ktunnel to expose the local conductor API into the cluster."""
        self._clean_stale_ktunnel()
        logger.info(f"Starting ktunnel: exposing localhost:8000 as {KTUNNEL_SERVICE}.{KTUNNEL_NAMESPACE}.svc")
        self._ktunnel_proc = subprocess.Popen(
            ["ktunnel", "expose", "-n", KTUNNEL_NAMESPACE, KTUNNEL_SERVICE, KTUNNEL_PORT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # Give ktunnel time to establish the tunnel
        time.sleep(10)
        if self._ktunnel_proc.poll() is not None:
            output = self._ktunnel_proc.stdout.read() if self._ktunnel_proc.stdout else ""
            raise RuntimeError(f"ktunnel exited unexpectedly: {output}")
        logger.info("ktunnel is running")

    def _stop_ktunnel(self):
        """Stop the ktunnel process and clean up its K8s resources."""
        if self._ktunnel_proc and self._ktunnel_proc.poll() is None:
            logger.info("Stopping ktunnel...")
            self._ktunnel_proc.terminate()
            try:
                self._ktunnel_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._ktunnel_proc.kill()
                self._ktunnel_proc.wait()
            self._ktunnel_proc = None
            logger.info("ktunnel stopped")

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
                f"API_HOSTNAME={KTUNNEL_SERVICE}.{KTUNNEL_NAMESPACE}.svc.cluster.local",
                "API_PORT=8000",
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
        Helm.uninstall(
            release_name=SATELLITE_RELEASE,
            namespace=SATELLITE_NAMESPACE,
        )
