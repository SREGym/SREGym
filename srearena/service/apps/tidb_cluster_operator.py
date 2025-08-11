import json
import time
import subprocess
import os

class TiDBClusterDeployer:
    def __init__(self, metadata_path):
        with open(metadata_path, "r") as f:
            self.metadata = json.load(f)

        self.name = self.metadata["Name"]
        self.namespace_tidb_cluster = self.metadata["K8S Config"]["namespace"]
        self.cluster_config_url = self.metadata["K8S Config"]["config_url"]

        # Helm Operator config details
        self.operator_namespace = self.metadata["Helm Operator Config"]["namespace"]
        self.operator_release_name = self.metadata["Helm Operator Config"]["release_name"]
        self.operator_chart = self.metadata["Helm Operator Config"]["chart_path"]
        self.operator_version = self.metadata["Helm Operator Config"]["version"]

        # **IMPORTANT: local values.yaml path**
        # Adjust this path as needed (path to your local values.yaml)
        self.operator_values_path = os.path.join(os.path.dirname(__file__), "tidb-operator", "values.yaml")

    def run_cmd(self, cmd):
        print(f"Running: {cmd}")
        subprocess.run(cmd, shell=True, check=True)

    def create_namespace(self, ns):
        self.run_cmd(f"kubectl create ns {ns} --dry-run=client -o yaml | kubectl apply -f -")

    def install_operator_with_values(self):
        print(f"Installing/upgrading TiDB Operator via Helm in namespace '{self.operator_namespace}'...")
        self.create_namespace(self.operator_namespace)

        # Add pingcap repo if needed (ignore error)
        subprocess.run("helm repo add pingcap https://charts.pingcap.org", shell=True)

        self.run_cmd("helm repo update")

        # Helm install/upgrade command using local values.yaml
        self.run_cmd(
            f"helm upgrade --install {self.operator_release_name} {self.operator_chart} "
            f"--version {self.operator_version} -n {self.operator_namespace} "
            f"--create-namespace -f {self.operator_values_path}"
        )

    def wait_for_operator_ready(self):
        print("Waiting for tidb-controller-manager pod to be running...")
        label = "app.kubernetes.io/component=controller-manager"
        for _ in range(24):
            try:
                status = subprocess.check_output(
                    f"kubectl get pods -n {self.operator_namespace} -l {label} -o jsonpath='{{.items[0].status.phase}}'",
                    shell=True,
                ).decode().strip()
                if status == "Running":
                    print("tidb-controller-manager pod is running.")
                    return
            except subprocess.CalledProcessError:
                pass
            print("Pod not ready yet, retrying in 5 seconds...")
            time.sleep(5)
        raise RuntimeError("Timeout waiting for tidb-controller-manager pod")

    def deploy_tidb_cluster(self):
        print(f"Creating TiDB cluster namespace '{self.namespace_tidb_cluster}'...")
        self.create_namespace(self.namespace_tidb_cluster)
        print(f"Deploying TiDB cluster manifest from {self.cluster_config_url}...")
        self.run_cmd(f"kubectl apply -f {self.cluster_config_url} -n {self.namespace_tidb_cluster}")

    def deploy_all(self):
        print(f"Starting deployment: {self.name}")
        self.create_namespace(self.namespace_tidb_cluster)
        self.install_operator_with_values()
        self.wait_for_operator_ready()
        self.deploy_tidb_cluster()
        print("Deployment complete.")

if __name__ == "__main__":
    # Replace path below with your actual JSON config path
    deployer = TiDBClusterDeployer("path/to/metadata.json")
    deployer.deploy_all()
