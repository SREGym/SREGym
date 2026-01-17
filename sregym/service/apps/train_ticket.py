"""Interface to the Train Ticket application"""

import os
import tempfile
import time
from pathlib import Path

from sregym.generators.workload.locust import LocustWorkloadManager
from sregym.paths import TARGET_MICROSERVICES, TRAIN_TICKET_METADATA
from sregym.service.apps.base import Application
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl


class TrainTicket(Application):
    def __init__(self):
        super().__init__(str(TRAIN_TICKET_METADATA))
        self.load_app_json()
        self.kubectl = KubeCtl()
        self.workload_manager = None

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.frontend_service = None
        self.frontend_port = None

    def deploy(self):
        if self._is_train_ticket_deployed():
            print(
                f"[TrainTicket] Skipping deployment: train-ticket app is already deployed in namespace {self.namespace}"
            )
            return

        if self.namespace:
            self.kubectl.create_namespace_if_not_exist(self.namespace)

        Helm.install(**self.helm_configs)
        self.kubectl.wait_for_job_completion(job_name="train-ticket-deploy", namespace="train-ticket", timeout=1200)

        self._deploy_flagd_infrastructure()
        self._deploy_load_generator()

    def delete(self):
        """Delete the Helm configurations."""
        # Helm.uninstall(**self.helm_configs) # Don't helm uninstall until cleanup job is fixed on train-ticket
        if self.namespace:
            self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)

    def _is_train_ticket_deployed(self):
        """Check if the train-ticket app is currently deployed."""
        try:

            # Check if the namespace exists
            namespace_exists = self.kubectl.exec_command(f"kubectl get namespace {self.namespace}")
            if "not found" in namespace_exists or "No resources found" in namespace_exists:
                return False

            # Check if train-ticket deployment exists
            deployment_exists = self.kubectl.exec_command(f"kubectl get deployment -n {self.namespace}")
            if "No resources found" in deployment_exists or not deployment_exists.strip():
                return False

            return True
        except Exception as e:
            print(f"[TrainTicket] Warning: Failed to check deployment status: {e}")
            return False

    def cleanup(self):
        """Cleanup the train-ticket application."""
        # Helm.uninstall(**self.helm_configs)
        if self.namespace:
            self.kubectl.delete_namespace(self.namespace)

    def create_workload(self):
        """Create workload manager for log collection (like astronomy shop)."""
        self.wrk = LocustWorkloadManager(
            namespace=self.namespace,
            locust_url="load-generator:8089",
        )

    def start_workload(self):
        """Start workload log collection (like astronomy shop)."""
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()
        print("[TrainTicket] Workload log collection started")

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()
            print("[TrainTicket] Workload log collection stopped")

    def _deploy_flagd_infrastructure(self):
        try:
            flagd_templates_path = TARGET_MICROSERVICES / "train-ticket" / "templates"

            if (flagd_templates_path / "flagd-deployment.yaml").exists():
                result = self.kubectl.exec_command(f"kubectl apply -f {flagd_templates_path / 'flagd-deployment.yaml'}")
                print(f"[TrainTicket] Deployed flagd service: {result}")

            if (flagd_templates_path / "flagd-config.yaml").exists():
                result = self.kubectl.exec_command(f"kubectl apply -f {flagd_templates_path / 'flagd-config.yaml'}")
                print(f"[TrainTicket] Deployed flagd ConfigMap: {result}")

            print(f"[TrainTicket] flagd infrastructure deployed successfully")

        except Exception as e:
            print(f"[TrainTicket] Warning: Failed to deploy flagd infrastructure: {e}")

    def _deploy_load_generator(self):
        try:

            locustfile_path = Path(__file__).parent.parent.parent / "resources" / "trainticket" / "locustfile.py"

            if locustfile_path.exists():
                result = self.kubectl.exec_command(
                    f"kubectl create configmap locustfile-config --from-file=locustfile.py={locustfile_path} -n {self.namespace} --dry-run=client -o yaml | kubectl apply -f -"
                )
                print(f"[TrainTicket] Created ConfigMap from file: {result}")

            deployment_path = (
                Path(__file__).parent.parent.parent / "resources" / "trainticket" / "locust-deployment.yaml"
            )

            if deployment_path.exists():
                with open(deployment_path, "r") as f:
                    content = f.read()

                with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
                    tmp.write(content)
                    temp_path = tmp.name

                result = self.kubectl.exec_command(f"kubectl apply -f {temp_path}")
                os.unlink(temp_path)
                print(f"[TrainTicket] Deployed load generator: {result}")

            print("[TrainTicket] Load generator deployed with auto-start")

        except Exception as e:
            print(f"[TrainTicket] Warning: Failed to deploy load generator: {e}")


# if __name__ == "__main__":
#     app = TrainTicket()
#     app.deploy()
#     app.delete()
