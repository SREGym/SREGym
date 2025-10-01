import time

from srearena.generators.workload.wrk2 import Wrk2, Wrk2WorkloadManager
from srearena.observer.trace_api import TraceAPI
from srearena.paths import FAULT_SCRIPTS, HOTEL_RES_METADATA, TARGET_MICROSERVICES
from srearena.service.apps.base import Application
from srearena.service.apps.helpers import get_frontend_url
from srearena.service.kubectl import KubeCtl


class HotelReservation(Application):
    def __init__(self):
        super().__init__(HOTEL_RES_METADATA)
        self.kubectl = KubeCtl()
        self.trace_api = None
        self.trace_api = None
        self.script_dir = FAULT_SCRIPTS
        self.helm_deploy = False

        self.load_app_json()

        self.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.frontend_service = metadata.get("frontend_service", "frontend")
        self.frontend_port = metadata.get("frontend_port", 5000)

    def create_configmaps(self):
        """Create configmaps for the hotel reservation application."""
        self.kubectl.create_or_update_configmap(
            name="mongo-rate-script",
            namespace=self.namespace,
            data=self._prepare_configmap_data(["k8s-rate-mongo.sh"]),
        )

        self.kubectl.create_or_update_configmap(
            name="mongo-geo-script",
            namespace=self.namespace,
            data=self._prepare_configmap_data(["k8s-geo-mongo.sh"]),
        )

        script_files = [
            "revoke-admin-rate-mongo.sh",
            "revoke-mitigate-admin-rate-mongo.sh",
            "remove-admin-mongo.sh",
            "remove-mitigate-admin-rate-mongo.sh",
        ]

        self.kubectl.create_or_update_configmap(
            name="failure-admin-rate",
            namespace=self.namespace,
            data=self._prepare_configmap_data(script_files),
        )

        script_files = [
            "revoke-admin-geo-mongo.sh",
            "revoke-mitigate-admin-geo-mongo.sh",
            "remove-admin-mongo.sh",
            "remove-mitigate-admin-geo-mongo.sh",
        ]

        self.kubectl.create_or_update_configmap(
            name="failure-admin-geo",
            namespace=self.namespace,
            data=self._prepare_configmap_data(script_files),
        )

    def deploy(self):
        """Deploy the Kubernetes configurations."""
        print(f"Deploying Kubernetes configurations in namespace: {self.namespace}")
        self.create_namespace()
        self.create_configmaps()
        self.kubectl.apply_configs(self.namespace, self.k8s_deploy_path)
        self.kubectl.wait_for_ready(self.namespace)
        self.trace_api = TraceAPI(self.namespace)
        self.trace_api.start_port_forward()

    def delete(self):
        """Delete the configmap."""
        self.kubectl.delete_configs(self.namespace, self.k8s_deploy_path)

    def cleanup(self):
        """Delete the entire namespace for the hotel reservation application."""
        if self.trace_api:
            self.trace_api.stop_port_forward()
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        pvs = self.kubectl.exec_command(
            "kubectl get pv --no-headers | grep 'hotel-reservation' | awk '{print $1}'"
        ).splitlines()

        for pv in pvs:
            # Check if the PV is in a 'Terminating' state and remove the finalizers if necessary
            self._remove_pv_finalizers(pv)
            delete_command = f"kubectl delete pv {pv}"
            delete_result = self.kubectl.exec_command(delete_command)
            print(f"Deleted PersistentVolume {pv}: {delete_result.strip()}")
        time.sleep(5)

        if hasattr(self, "wrk"):
            # self.wrk.stop()
            self.kubectl.delete_job(label="job=workload", namespace=self.namespace)

    def _remove_pv_finalizers(self, pv_name: str):
        """Remove finalizers from the PersistentVolume to prevent it from being stuck in a 'Terminating' state."""
        # Patch the PersistentVolume to remove finalizers if it is stuck
        patch_command = f'kubectl patch pv {pv_name} -p \'{{"metadata":{{"finalizers":null}}}}\''
        _ = self.kubectl.exec_command(patch_command)

    # helper methods
    def _prepare_configmap_data(self, script_files: list) -> dict:
        data = {}
        for file in script_files:
            data[file] = self._read_script(f"{self.script_dir}/{file}")
        return data

    def _read_script(self, file_path: str) -> str:
        with open(file_path, "r") as file:
            return file.read()

    def create_workload(
        self, rate: int = 100, dist: str = "exp", connections: int = 100, duration: int = 30, threads: int = 3
    ):
        self.wrk = Wrk2WorkloadManager(
            wrk=Wrk2(
                rate=rate,
                dist=dist,
                connections=connections,
                duration=duration,
                threads=threads,
                namespace=self.namespace,
            ),
            payload_script=self.payload_script,
            url=f"{{placeholder}}",
            namespace=self.namespace,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.url = get_frontend_url(self)
        self.wrk.start()
