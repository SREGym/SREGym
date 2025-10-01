"""Interface to the OpenTelemetry Astronomy Shop application"""

import time

from srearena.generators.workload.locust import LocustWorkloadManager
from srearena.observer.trace_api import TraceAPI
from srearena.paths import ASTRONOMY_SHOP_METADATA
from srearena.service.apps.base import Application
from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl


class AstronomyShop(Application):
    def __init__(self):
        super().__init__(ASTRONOMY_SHOP_METADATA)
        self.load_app_json()
        self.kubectl = KubeCtl()
        self.trace_api = None
        self.create_namespace()

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.frontend_service = "frontend-proxy"
        self.frontend_port = 8080

    def deploy(self):
        """Deploy the Helm configurations."""
        self.kubectl.create_namespace_if_not_exist(self.namespace)

        self.helm_configs["extra_args"] = [
            "--set-string",
            "components.load-generator.envOverrides[0].name=LOCUST_BROWSER_TRAFFIC_ENABLED",
            "--set-string",
            "components.load-generator.envOverrides[0].value=false",
        ]

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.helm_configs["namespace"])
        self.trace_api = TraceAPI(self.namespace)
        self.trace_api.start_port_forward()

    def delete(self):
        """Delete the Helm configurations."""
        Helm.uninstall(**self.helm_configs)
        self.kubectl.delete_namespace(self.helm_configs["namespace"])
        self.kubectl.wait_for_namespace_deletion(self.namespace)

    def cleanup(self):
        if self.trace_api:
            self.trace_api.stop_port_forward()
        Helm.uninstall(**self.helm_configs)
        self.kubectl.delete_namespace(self.helm_configs["namespace"])

        if hasattr(self, "wrk"):
            # self.wrk.stop()
            self.kubectl.delete_job(label="job=workload", namespace=self.namespace)

    def create_workload(self):
        self.wrk = LocustWorkloadManager(
            namespace=self.namespace,
            locust_url="load-generator:8089",
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()


# Run this code to test installation/deletion
# if __name__ == "__main__":
#     shop = AstronomyShop()
#     shop.deploy()
#     shop.delete()
