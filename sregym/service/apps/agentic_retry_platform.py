"""Interface to the Agentic Retry Platform application."""

from __future__ import annotations

import logging

from sregym.paths import AGENTIC_RETRY_PLATFORM_METADATA
from sregym.service.apps.base import Application
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.application.agentic_retry_platform")


class AgenticRetryPlatform(Application):
    """Application representation for Agentic Retry Platform."""

    def __init__(self, embedded: bool = True):
        super().__init__(str(AGENTIC_RETRY_PLATFORM_METADATA))
        self.load_app_json()
        self.embedded = embedded
        self.kubectl = None
        if not embedded:
            try:
                self.kubectl = KubeCtl()
            except SystemExit:
                self.kubectl = None
                self.embedded = True
        self.workload = None

    def deploy(self):
        """Deploy application components on Kubernetes cluster."""
        logger.info(f"Deploying {self.name} in namespace {self.namespace} (embedded={self.embedded})")
        if self.kubectl is None:
            try:
                self.kubectl = KubeCtl()
            except SystemExit:
                logger.warning("No cluster available; deploy in embedded mode")
                return
        self.create_namespace()
        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.helm_configs["namespace"])

    def cleanup(self):
        """Clean up deployed application resources."""
        logger.info(f"Cleaning up {self.name} in namespace {self.namespace}")
        if self.kubectl is not None:
            Helm.uninstall(**self.helm_configs)
            self.kubectl.delete_namespace(self.namespace)
        if self.workload is not None:
            self.workload.stop()

    def get_app_summary(self) -> str:
        return (
            f"App Name: {self.name}\n"
            f"Namespace: {self.namespace}\n"
            f"Description: Autonomous Agentic Retry Platform with multi-layer retries across "
            f"agent orchestrator, tool gateway, and transport layers targeting PgBouncer and PostgreSQL."
        )
