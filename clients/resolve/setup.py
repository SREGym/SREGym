"""Resolve AI infrastructure setup and teardown.

Manages the ktunnel reverse tunnel and Resolve satellite Helm chart
that are required for Resolve AI to interact with SREGym.
"""

import logging
import subprocess
import time

logger = logging.getLogger(__name__)

SATELLITE_RELEASE = "resolve-satellite"
SATELLITE_CHART = "oci://registry-1.docker.io/resolveaihq/satellite-chart"
SATELLITE_VALUES = "resolve-values.yaml"
KTUNNEL_NAMESPACE = "sregym"
KTUNNEL_SERVICE = "conductor-api"
KTUNNEL_PORT = "8000:8000"


class ResolveSetup:
    def __init__(self):
        self._ktunnel_proc: subprocess.Popen | None = None

    def start(self):
        """Start ktunnel and install the Resolve satellite."""
        self._start_ktunnel()
        self._install_satellite()

    def stop(self):
        """Tear down ktunnel and uninstall the Resolve satellite."""
        self._uninstall_satellite()
        self._stop_ktunnel()

    def _start_ktunnel(self):
        """Start ktunnel to expose the local conductor API into the cluster."""
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

    def _install_satellite(self):
        """Install the Resolve satellite Helm chart."""
        logger.info(f"Installing Resolve satellite chart ({SATELLITE_RELEASE})...")
        result = subprocess.run(
            [
                "helm",
                "install",
                SATELLITE_RELEASE,
                SATELLITE_CHART,
                "--values",
                SATELLITE_VALUES,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # If already installed, try upgrade instead
            if "cannot re-use" in result.stderr:
                logger.info("Satellite already installed, upgrading...")
                result = subprocess.run(
                    [
                        "helm",
                        "upgrade",
                        SATELLITE_RELEASE,
                        SATELLITE_CHART,
                        "--values",
                        SATELLITE_VALUES,
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Helm upgrade failed: {result.stderr}")
            else:
                raise RuntimeError(f"Helm install failed: {result.stderr}")
        logger.info("Resolve satellite installed")

    def _uninstall_satellite(self):
        """Uninstall the Resolve satellite Helm chart."""
        logger.info(f"Uninstalling Resolve satellite ({SATELLITE_RELEASE})...")
        result = subprocess.run(
            ["helm", "uninstall", SATELLITE_RELEASE],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Helm uninstall warning: {result.stderr}")
        else:
            logger.info("Resolve satellite uninstalled")
