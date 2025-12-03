import logging
from time import sleep

from sregym.paths import BASE_PARENT_DIR, FAULT_SCRIPTS, MONGODB_CLUSTER_METADATA
from sregym.service.apps.base import Application
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

local_logger = logging.getLogger("all.application")
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)


class MongoDBApplication(Application):
    """MongoDB Cluster Application Class.

    A MongoDB cluster deployed via the MongoDB Community Operator.
    """

    def __init__(self):
        super().__init__(MONGODB_CLUSTER_METADATA)
        self.kubectl = KubeCtl()
        self.script_dir = FAULT_SCRIPTS

        # Reload to get the new paths we just added to metadata
        self.load_app_json()
        self.cluster_cr_path = self.metadata.get("cluster_cr_path")
        self.secret_path = self.metadata.get("secret_path")

    def load_app_json(self):
        super().load_app_json()
        self.metadata = self.get_app_json()

    def deploy(self):
        """Deploy the Operator via Helm, then the Cluster CR."""
        local_logger.info("Deploying MongoDB Operator via Helm in namespace: %s", self.namespace)
        self.create_namespace()

        # 0. Install CRDs (Required before Helm install)
        crd_url = "https://raw.githubusercontent.com/mongodb/mongodb-kubernetes/1.6.0/public/crds.yaml"
        local_logger.info(f"Applying CRDs from {crd_url}...")
        self.kubectl.exec_command(f"kubectl apply -f {crd_url}")

        # 1. Deploy Operator via Helm
        repo_name = self.helm_configs.get("repo_name")
        repo_url = self.helm_configs.get("repo_url")
        chart_name = self.helm_configs.get("chart_name")
        release_name = self.helm_configs.get("release_name")

        try:
            Helm.add_repo(repo_name, repo_url)
        except Exception as e:
            local_logger.warning(f"Failed to add repo {repo_name}: {e}")

        local_logger.info(f"Installing Operator {chart_name}...")
        helm_args = {
            "release_name": release_name,
            "chart_path": chart_name,
            "namespace": self.namespace,
            "remote_chart": True,
        }
        Helm.install(**helm_args)

        # Wait for Operator to be ready
        local_logger.info("Waiting for Operator to be ready...")
        sleep(10)  # Give Helm a moment to register resources
        self.kubectl.wait_for_ready(self.namespace)
        print("MongoDB Operator deployed")

        # 2. Deploy Secret and Cluster CR
        # Resolve absolute paths based on workspace root
        secret_full_path = str(BASE_PARENT_DIR / self.secret_path)
        cr_full_path = str(BASE_PARENT_DIR / self.cluster_cr_path)

        local_logger.info("Deploying MongoDB Cluster Secret...")
        self.kubectl.apply_configs(self.namespace, secret_full_path)

        local_logger.info("Deploying MongoDB Cluster CR...")
        self.kubectl.apply_configs(self.namespace, cr_full_path)

        local_logger.info("Waiting for MongoDB Cluster to be ready...")
        # This might take a while as the operator provisions the StatefulSet
        sleep(15)
        self.kubectl.wait_for_ready(self.namespace, max_wait=600)

        print("MongoDB Cluster deployed via Operator")

    def start_workload(self):
        pass

    def delete(self):
        """Delete the resources."""
        # 1. Delete CR (triggers operator to clean up)
        local_logger.info("Deleting MongoDB Cluster CR...")
        cr_full_path = str(BASE_PARENT_DIR / self.cluster_cr_path)
        self.kubectl.delete_configs(self.namespace, cr_full_path)

        # 2. Uninstall Operator
        release_name = self.helm_configs.get("release_name")
        local_logger.info(f"Uninstalling Operator {release_name}...")
        Helm.uninstall(release_name=release_name, namespace=self.namespace)

        # 3. Delete CRD to avoid Helm conflicts on re-install
        # Helm uninstall does not delete CRDs by default.
        local_logger.info("Deleting MongoDB CRDs to ensure clean slate...")
        crd_url = "https://raw.githubusercontent.com/mongodb/mongodb-kubernetes/1.6.0/public/crds.yaml"
        self.kubectl.exec_command(f"kubectl delete -f {crd_url} --ignore-not-found")

    def cleanup(self):
        """Delete the entire namespace."""
        super().cleanup()


if __name__ == "__main__":
    app = MongoDBApplication()
    app.delete()
    app.deploy()
    input("Press Enter to continue...")
    app.delete()
