import contextlib
import logging
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import yaml

from sregym.generators.workload.wrk2 import Wrk2, Wrk2WorkloadManager
from sregym.paths import FAULT_SCRIPTS, HOTEL_RES_METADATA, TARGET_MICROSERVICES
from sregym.service.apps.base import Application
from sregym.service.apps.helpers import get_frontend_url
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.application")
logger.propagate = True
logger.setLevel(logging.DEBUG)

HOTEL_RESERVATION_APPLICATION_IMAGE = "ghcr.io/sregym/hotel-reservation:latest"


class HotelReservation(Application):
    def __init__(
        self,
        mount_failure_scripts: bool = True,
        deployment_env_overrides: dict[str, dict[str, dict[str, str]]] | None = None,
    ):
        super().__init__(HOTEL_RES_METADATA)
        self.kubectl = KubeCtl()
        self.script_dir = FAULT_SCRIPTS
        self.helm_deploy = False
        self.mount_failure_scripts = mount_failure_scripts
        self.deployment_env_overrides = deployment_env_overrides or {}

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

    # Script file lists for failure configmaps (used by populate/clear lifecycle)
    FAILURE_ADMIN_RATE_SCRIPTS = [
        "revoke-admin-rate-mongo.sh",
        "revoke-mitigate-admin-rate-mongo.sh",
        "remove-admin-mongo.sh",
        "remove-mitigate-admin-rate-mongo.sh",
    ]
    FAILURE_ADMIN_GEO_SCRIPTS = [
        "revoke-admin-geo-mongo.sh",
        "revoke-mitigate-admin-geo-mongo.sh",
        "remove-admin-mongo.sh",
        "remove-mitigate-admin-geo-mongo.sh",
    ]

    def create_configmaps(self):
        """Create configmaps for the hotel reservation application.

        Note: failure-admin-{geo,rate} are created as standalone K8s objects (not mounted
        into pods). They serve as noise/red herrings for unrelated faults.
        """
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

    def populate_failure_configmaps(self):
        """Create/fill failure-admin-{geo,rate} configmaps with fault scripts (noise)."""
        self.kubectl.create_or_update_configmap(
            name="failure-admin-rate",
            namespace=self.namespace,
            data=self._prepare_configmap_data(self.FAILURE_ADMIN_RATE_SCRIPTS),
        )
        self.kubectl.create_or_update_configmap(
            name="failure-admin-geo",
            namespace=self.namespace,
            data=self._prepare_configmap_data(self.FAILURE_ADMIN_GEO_SCRIPTS),
        )

    def clear_failure_configmaps(self):
        """Empty failure-admin-{geo,rate} configmaps (best-effort).

        Removes all script data so agents cannot read fault details.
        """
        try:
            self.kubectl.create_or_update_configmap(
                name="failure-admin-rate",
                namespace=self.namespace,
                data={},
            )
            self.kubectl.create_or_update_configmap(
                name="failure-admin-geo",
                namespace=self.namespace,
                data={},
            )
        except Exception as e:
            logger.warning(f"Best-effort clearing of failure configmaps failed: {e}")

    def _patch_mongo_failure_script_mounts(self):
        """Patch mongodb-geo and mongodb-rate deployments to mount failure configmaps at /scripts.

        Uses JSON patch (append-only) to add the volume and volumeMount without
        needing to re-specify existing volumes — safer if the YAML changes.
        """
        patches = [
            ("mongodb-geo", "failure-admin-geo"),
            ("mongodb-rate", "failure-admin-rate"),
        ]
        for deployment_name, configmap_name in patches:
            patch_cmd = (
                f"kubectl patch deployment {deployment_name} -n {self.namespace} --type=json -p="
                "'["
                f'{{"op":"add","path":"/spec/template/spec/volumes/-",'
                f'"value":{{"name":"failure-script","configMap":{{"name":"{configmap_name}"}}}}}}'
                f',{{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-",'
                f'"value":{{"name":"failure-script","mountPath":"/scripts"}}}}'
                "]'"
            )
            self.kubectl.exec_command(patch_cmd)

    @contextlib.contextmanager
    def _rendered_deployment_configs(self) -> Iterator[Path]:
        """Render problem-specific env values before Kubernetes sees a Deployment.

        Most problems use the application manifests unchanged. A problem that
        needs a different, initially healthy runtime policy can provide exact
        Deployment/container overrides. Rendering a temporary manifest tree
        avoids a setup rollout and its misleading ReplicaSet history.
        """
        if not self.deployment_env_overrides:
            yield Path(self.k8s_deploy_path)
            return

        with tempfile.TemporaryDirectory(prefix="hotel-reservation-config-") as temporary_dir:
            rendered_path = Path(temporary_dir) / "kubernetes"
            shutil.copytree(self.k8s_deploy_path, rendered_path)
            unmatched = {
                (deployment_name, container_name)
                for deployment_name, containers in self.deployment_env_overrides.items()
                for container_name in containers
            }

            for config_path in [*rendered_path.rglob("*.yaml"), *rendered_path.rglob("*.yml")]:
                with config_path.open() as config_file:
                    documents = list(yaml.safe_load_all(config_file))

                changed = False
                for document in documents:
                    if not isinstance(document, dict) or document.get("kind") != "Deployment":
                        continue
                    deployment_name = document.get("metadata", {}).get("name")
                    container_overrides = self.deployment_env_overrides.get(deployment_name)
                    if not container_overrides:
                        continue

                    containers = document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    for container in containers:
                        container_name = container.get("name")
                        values = container_overrides.get(container_name)
                        if not values:
                            continue
                        existing = [item for item in container.get("env", []) if item.get("name") not in values]
                        container["env"] = [
                            *existing,
                            *({"name": name, "value": str(value)} for name, value in values.items()),
                        ]
                        unmatched.discard((deployment_name, container_name))
                        changed = True

                if changed:
                    with config_path.open("w") as config_file:
                        yaml.safe_dump_all(documents, config_file, sort_keys=False)

            if unmatched:
                targets = ", ".join(
                    f"deployment/{deployment}:{container}" for deployment, container in sorted(unmatched)
                )
                raise RuntimeError(f"deployment environment override targets were not found: {targets}")

            yield rendered_path

    def deploy(self):
        """Deploy the Kubernetes configurations."""
        self.logger.info(f"Deploying Kubernetes configurations in namespace: {self.namespace}")
        self.create_namespace()
        self.create_configmaps()
        with self._rendered_deployment_configs() as config_path:
            self.kubectl.apply_configs(self.namespace, config_path)
        if self.mount_failure_scripts:
            self.populate_failure_configmaps()
            self._patch_mongo_failure_script_mounts()
        self.kubectl.wait_for_ready(self.namespace)

    def delete(self):
        """Delete the configmap."""
        self.kubectl.delete_configs(self.namespace, self.k8s_deploy_path)

    def cleanup(self):
        """Delete the entire namespace for the hotel reservation application."""
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
            logger.info(f"Deleted PersistentVolume {pv}: {delete_result.strip()}")
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
        with open(file_path) as file:
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
            url="{placeholder}",
            namespace=self.namespace,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.url = get_frontend_url(self)
        self.wrk.start()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()
