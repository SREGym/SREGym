"""Interface to the OpenTelemetry Astronomy Shop application"""

import json
import time
from typing import Any, Dict

from sregym.generators.workload.locust import LocustWorkloadManager
from sregym.observer.trace_api import TraceAPI
from sregym.paths import ASTRONOMY_SHOP_METADATA
from sregym.service.apps.base import Application
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


class AstronomyShop(Application):
    _FLAGD_CONFIGMAP = "flagd-config"
    _FLAGD_CONFIG_KEY = "demo.flagd.json"
    _FLAGD_DEPLOYMENT = "flagd"
    _VARIANT_ON = "on"
    _VARIANT_OFF = "off"

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

        self._FLAGD_CONFIGMAP = metadata.get("FlagdConfigMap", self._FLAGD_CONFIGMAP)
        self._FLAGD_CONFIG_KEY = metadata.get("FlagdConfigKey", self._FLAGD_CONFIG_KEY)
        self._FLAGD_DEPLOYMENT = metadata.get("FlagdDeployment", self._FLAGD_DEPLOYMENT)

    def deploy(self):
        """Deploy the Helm configurations."""
        self.kubectl.create_namespace_if_not_exist(self.namespace)

        self.helm_configs["extra_args"] = [
            # Disable bundled Prometheus to avoid ClusterRole conflict with central
            # Prometheus in the observe namespace (ClusterRoles are cluster-wide)
            "--set",
            "prometheus.enabled=false",
            "--set-string",
            "components.load-generator.envOverrides[0].name=LOCUST_BROWSER_TRAFFIC_ENABLED",
            "--set-string",
            "components.load-generator.envOverrides[0].value=false",
        ]

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.helm_configs["namespace"])
        self.trace_api = TraceAPI(self.namespace)
        self.trace_api.start_port_forward()

    def _read_flagd_config(self) -> Dict[str, Any]:
        """
        Returns contents of demo.flagd.json from the flagd config map.
        """
        raw = self.kubectl.exec_command(
            f"kubectl get configmap {self._FLAGD_CONFIGMAP} -n {self.namespace} -o json"
        )
        configmap = json.loads(raw)
        return json.loads(configmap["data"][self._FLAGD_CONFIG_KEY])

    def _write_flagd_config(self, config: Dict[str, Any]) -> None:
        """
        Writes demo.flagd.json to the flagd config map and restarts flagd.
        """
        updated_data = {self._FLAGD_CONFIG_KEY: json.dumps(config, indent=2)}
        self.kubectl.create_or_update_configmap(
            self._FLAGD_CONFIGMAP, self.namespace, updated_data
        )
        self.kubectl.exec_command(
            f"kubectl rollout restart deployment/{self._FLAGD_DEPLOYMENT} -n {self.namespace}"
        )
        # Avoid racy executions where ConfigMap is updated but the flagd pod is reloading
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self._FLAGD_DEPLOYMENT} -n {self.namespace} --timeout=60s"
        )

    def set_flag(self, flag_name: str, enabled: bool) -> None:
        """
        Enables/disables a flagd feature flag (on/off).
        """
        config = self._read_flagd_config()

        try:
            flag = config["flags"][flag_name]
        except KeyError as e:
            raise ValueError(
                f"Feature flag '{flag_name}' not found in flagd config."
            ) from e

        desired = self._VARIANT_ON if enabled else self._VARIANT_OFF
        if flag.get("defaultVariant") == desired:
            return

        flag["defaultVariant"] = self._VARIANT_ON if enabled else self._VARIANT_OFF
        self._write_flagd_config(config)

    def get_flag_status(self, flag_name: str) -> bool:
        """
        Returns True if the flag is 'on', or False otherwise.
        """
        config = self._read_flagd_config()

        try:
            flag = config["flags"][flag_name]
            return flag.get("defaultVariant") == self._VARIANT_ON
        except KeyError as e:
            raise ValueError(
                f"Feature flag '{flag_name}' not found in flagd config."
            ) from e

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

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()


# Run this code to test installation/deletion
# if __name__ == "__main__":
#     shop = AstronomyShop()
#     shop.deploy()
#     shop.delete()
