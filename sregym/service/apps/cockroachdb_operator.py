import logging
from time import sleep

from sregym.paths import COCKROACH_DB_CLUSTER_METADATA, FAULT_SCRIPTS, TARGET_MICROSERVICES
from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl

local_logger = logging.getLogger("all.application")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class CockroachDBApplication(Application):

    def __init__(self):
        super().__init__(COCKROACH_DB_CLUSTER_METADATA)
        self.kubectl = KubeCtl()
        self.script_dir = FAULT_SCRIPTS
        self.helm_deploy = False

        self.load_app_json()

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.k8s_deploy_path = TARGET_MICROSERVICES / metadata["K8S Deploy Path"]
        self.cr_path = TARGET_MICROSERVICES / metadata["CR Path"]

    def deploy(self):
        """Deploy the Kubernetes configurations."""
        local_logger.info("Deploying Kubernetes configurations in namespace: %s", self.namespace)
        self.create_namespace()
        self.kubectl.apply_configs(self.namespace, self.k8s_deploy_path)
        self.kubectl.wait_for_ready(self.namespace)
        sleep(10)  # Wait for operator to be fully up
        self.kubectl.apply_configs(self.namespace, self.cr_path)
        sleep(5)
        self.kubectl.wait_for_ready(self.namespace)

        # Delete operator after the cluster is up
        # self.kubectl.delete_configs(self.namespace, self.k8s_deploy_path)

    def start_workload(self):
        pass

    def delete(self):
        """Delete the configmap."""
        self.kubectl.delete_configs(self.namespace, self.k8s_deploy_path)

    def cleanup(self):
        """Delete the entire namespace for the hotel reservation application."""
        self.kubectl.delete_namespace(self.namespace)
        self.kubectl.wait_for_namespace_deletion(self.namespace)
        self.kubectl.delete_job(label="job=workload", namespace=self.namespace)
