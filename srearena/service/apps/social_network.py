"""Interface to the social network application from DeathStarBench"""

import time

from srearena.generators.workload.wrk2 import Wrk2, Wrk2WorkloadManager
from srearena.observer.trace_api import TraceAPI
from srearena.paths import SOCIAL_NETWORK_METADATA, TARGET_MICROSERVICES
from srearena.service.apps.base import Application
from srearena.service.apps.helpers import get_frontend_url
from srearena.service.helm import Helm
from srearena.service.kubectl import KubeCtl


class SocialNetwork(Application):
    def __init__(self):
        super().__init__(SOCIAL_NETWORK_METADATA)
        self.load_app_json()
        self.kubectl = KubeCtl()
        self.trace_api = None
        self.local_tls_path = TARGET_MICROSERVICES / "socialNetwork/helm-chart/socialnetwork"

        self.payload_script = TARGET_MICROSERVICES / "socialNetwork/wrk2/scripts/social-network/mixed-workload.lua"

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.frontend_service = metadata.get("frontend_service", "nginx-thrift")
        self.frontend_port = metadata.get("frontend_port", 8080)

    def create_tls_secret(self):
        check_sec = f"kubectl get secret mongodb-tls -n {self.namespace}"
        result = self.kubectl.exec_command(check_sec)
        if "notfound" in result.lower():
            create_sec_command = (
                f"kubectl create secret generic mongodb-tls "
                f"--from-file=tls.pem={self.local_tls_path}/tls.pem "
                f"--from-file=ca.crt={self.local_tls_path}/ca.crt "
                f"-n {self.namespace}"
            )
            create_result = self.kubectl.exec_command(create_sec_command)
            print(f"TLS secret created: {create_result.strip()}")
        else:
            print("TLS secret already exists. Skipping creation.")

    def deploy(self):
        """Deploy the Helm configurations with architecture-aware image selection."""
        self.create_namespace()
        self.create_tls_secret()
        node_architectures = self.kubectl.get_node_architectures()
        is_arm = any(arch in ["arm64", "aarch64"] for arch in node_architectures)

        if is_arm:
            # Use the ARM-compatible image for media-frontend
            if "extra_args" not in self.helm_configs:
                self.helm_configs["extra_args"] = []

            self.helm_configs["extra_args"].append(
                "--set media-frontend.container.image=jacksonarthurclark/media-frontend"
            )
            self.helm_configs["extra_args"].append("--set media-frontend.container.imageVersion=latest")

        Helm.install(**self.helm_configs)
        Helm.assert_if_deployed(self.helm_configs["namespace"])
        self.trace_api = TraceAPI(self.namespace)
        self.trace_api.start_port_forward()

    def delete(self):
        """Delete the Helm configurations."""
        Helm.uninstall(**self.helm_configs)

    def cleanup(self):
        """Delete the entire namespace for the social network application."""
        if self.trace_api:
            self.trace_api.stop_port_forward()
        Helm.uninstall(**self.helm_configs)

        if hasattr(self, "wrk"):
            # self.wrk.stop()
            self.kubectl.delete_job(label="job=workload")
        self.kubectl.delete_namespace(self.namespace)

    def create_workload(
        self, rate: int = 100, dist: str = "exp", connections: int = 3, duration: int = 10, threads: int = 3
    ):
        self.wrk = Wrk2WorkloadManager(
            wrk=Wrk2(rate=rate, dist=dist, connections=connections, duration=duration, threads=threads, namespace=self.namespace),
            payload_script=self.payload_script,
            url=f"{{placeholder}}/wrk2-api/post/compose",
            namespace=self.namespace,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.url = get_frontend_url(self) + "/wrk2-api/post/compose"
        self.wrk.start()
