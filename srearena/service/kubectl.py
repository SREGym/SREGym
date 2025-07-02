"""Interface to K8S controller service."""

import json
import subprocess
import time

try:
    from kubernetes import client, config
except ModuleNotFoundError as e:
    print("Your Kubeconfig is missing. Please set up a cluster.")
    exit(1)
from kubernetes.client.rest import ApiException
from rich.console import Console


class KubeCtl:
    def __init__(self):
        """Initialize the KubeCtl object and load the Kubernetes configuration."""
        try:
            config.load_kube_config()
        except Exception as e:
            print("Missing kubeconfig. Please set up a cluster.")
            exit(1)
        self.core_v1_api = client.CoreV1Api()
        self.apps_v1_api = client.AppsV1Api()

    def list_namespaces(self):
        """Return a list of all namespaces in the cluster."""
        return self.core_v1_api.list_namespace()

    def list_pods(self, namespace):
        """Return a list of all pods within a specified namespace."""
        return self.core_v1_api.list_namespaced_pod(namespace)

    def list_services(self, namespace):
        """Return a list of all services within a specified namespace."""
        return self.core_v1_api.list_namespaced_service(namespace)

    def get_cluster_ip(self, service_name, namespace):
        """Retrieve the cluster IP address of a specified service within a namespace."""
        service_info = self.core_v1_api.read_namespaced_service(service_name, namespace)
        return service_info.spec.cluster_ip  # type: ignore

    def get_container_runtime(self):
        """
        Retrieve the container runtime used by the cluster.
        If the cluster uses multiple container runtimes, the first one found will be returned.
        """
        for node in self.core_v1_api.list_node().items:
            for status in node.status.conditions:
                if status.type == "Ready" and status.status == "True":
                    return node.status.node_info.container_runtime_version

    def get_pod_name(self, namespace, label_selector):
        """Get the name of the first pod in a namespace that matches a given label selector."""
        pod_info = self.core_v1_api.list_namespaced_pod(namespace, label_selector=label_selector)
        return pod_info.items[0].metadata.name

    def get_pod_logs(self, pod_name, namespace):
        """Retrieve the logs of a specified pod within a namespace."""
        return self.core_v1_api.read_namespaced_pod_log(pod_name, namespace)

    def get_service_json(self, service_name, namespace, deserialize=True):
        """Retrieve the JSON description of a specified service within a namespace."""
        command = f"kubectl get service {service_name} -n {namespace} -o json"
        result = self.exec_command(command)

        return json.loads(result) if deserialize else result

    def get_deployment(self, name: str, namespace: str):
        """Fetch the deployment configuration."""
        return self.apps_v1_api.read_namespaced_deployment(name, namespace)

    def wait_for_ready(self, namespace, sleep=2, max_wait=500):
        """Wait for all pods in a namespace to be in a Ready state before proceeding."""

        console = Console()
        console.log(f"[bold yellow]Waiting for all pods in namespace '{namespace}' to be ready...")

        with console.status("[bold green]Waiting for pods to be ready...") as status:
            wait = 0

            while wait < max_wait:
                try:
                    pod_list = self.list_pods(namespace)

                    if pod_list.items:
                        ready_pods = [
                            pod
                            for pod in pod_list.items
                            if pod.status.container_statuses and all(cs.ready for cs in pod.status.container_statuses)
                        ]

                        if len(ready_pods) == len(pod_list.items):
                            console.log(f"[bold green]All pods in namespace '{namespace}' are ready.")
                            return

                except Exception as e:
                    console.log(f"[red]Error checking pod statuses: {e}")

                time.sleep(sleep)
                wait += sleep

            raise Exception(
                f"[red]Timeout: Not all pods in namespace '{namespace}' reached the Ready state within {max_wait} seconds."
            )

    def wait_for_namespace_deletion(self, namespace, sleep=2, max_wait=300):
        """Wait for a namespace to be fully deleted before proceeding."""

        console = Console()
        console.log(f"[bold yellow]Waiting for namespace '{namespace}' to be deleted...")

        with console.status("[bold yellow]Waiting for namespace deletion...") as status:
            wait = 0

            while wait < max_wait:
                try:
                    self.core_v1_api.read_namespace(name=namespace)
                except Exception as e:
                    console.log(f"[bold green]Namespace '{namespace}' has been deleted.")
                    return

                time.sleep(sleep)
                wait += sleep

            raise Exception(f"[red]Timeout: Namespace '{namespace}' was not deleted within {max_wait} seconds.")

    def is_ready(self, pod):
        phase = pod.status.phase or ""
        container_statuses = pod.status.container_statuses or []
        conditions = pod.status.conditions or []

        if phase in ["Succeeded", "Failed"]:
            return True

        if phase == "Running":
            if all(cs.ready for cs in container_statuses):
                return True

        for cs in container_statuses:
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason
                if reason == "CrashLoopBackOff":
                    return True

        if phase == "Pending":
            for cond in conditions:
                if cond.type == "PodScheduled" and cond.status == "False":
                    return True

        return False

    def wait_for_stable(self, namespace: str, sleep: int = 2, max_wait: int = 300):
        console = Console()
        console.log(f"[bold yellow]Waiting for namespace '{namespace}' to be stable...")

        with console.status("[bold yellow]Waiting for pods to be stable...") as status:
            wait = 0

            while wait < max_wait:
                try:
                    pod_list = self.list_pods(namespace)

                    if pod_list.items:

                        if all(self.is_ready(pod) for pod in pod_list.items):
                            console.log(f"[bold green]All pods in namespace '{namespace}' are stable.")
                            return
                except Exception as e:
                    console.log(f"[red]Error checking pod statuses: {e}")

                time.sleep(sleep)
                wait += sleep

            raise Exception(f"[red]Timeout: Namespace '{namespace}' was not deleted within {max_wait} seconds.")

    def update_deployment(self, name: str, namespace: str, deployment):
        """Update the deployment configuration."""
        return self.apps_v1_api.replace_namespaced_deployment(name, namespace, deployment)

    def patch_service(self, name, namespace, body):
        """Patch a Kubernetes service in a specified namespace."""
        try:
            api_response = self.core_v1_api.patch_namespaced_service(name, namespace, body)
            return api_response
        except ApiException as e:
            print(f"Exception when patching service: {e}\n")
            return None

    def create_configmap(self, name, namespace, data):
        """Create or update a configmap from a dictionary of data."""
        try:
            api_response = self.update_configmap(name, namespace, data)
            return api_response
        except ApiException as e:
            if e.status == 404:
                return self.create_new_configmap(name, namespace, data)
            else:
                print(f"Exception when updating configmap: {e}\n")
                print(f"Exception status code: {e.status}\n")
                return None

    def create_new_configmap(self, name, namespace, data):
        """Create a new configmap."""
        config_map = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(name=name),
            data=data,
        )
        try:
            return self.core_v1_api.create_namespaced_config_map(namespace, config_map)
        except ApiException as e:
            print(f"Exception when creating configmap: {e}\n")
            return None

    def create_or_update_configmap(self, name: str, namespace: str, data: dict):
        """Create a configmap if it doesn't exist, or update it if it does."""
        try:
            existing_configmap = self.core_v1_api.read_namespaced_config_map(name, namespace)
            # ConfigMap exists, update it
            existing_configmap.data = data
            self.core_v1_api.replace_namespaced_config_map(name, namespace, existing_configmap)
            print(f"ConfigMap '{name}' updated in namespace '{namespace}'")
        except ApiException as e:
            if e.status == 404:
                # ConfigMap doesn't exist, create it
                body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=name), data=data)
                self.core_v1_api.create_namespaced_config_map(namespace, body)
                print(f"ConfigMap '{name}' created in namespace '{namespace}'")
            else:
                print(f"Error creating/updating ConfigMap '{name}': {e}")

    def update_configmap(self, name, namespace, data):
        """Update existing configmap with the provided data."""
        config_map = client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(name=name),
            data=data,
        )
        try:
            return self.core_v1_api.replace_namespaced_config_map(name, namespace, config_map)
        except ApiException as e:
            print(f"Exception when updating configmap: {e}\n")
            return

    def apply_configs(self, namespace: str, config_path: str):
        """Apply Kubernetes configurations from a specified path to a namespace."""
        command = f"kubectl apply -Rf {config_path} -n {namespace}"
        self.exec_command(command)

    def delete_configs(self, namespace: str, config_path: str):
        """Delete Kubernetes configurations from a specified path in a namespace."""
        try:
            exists_resource = self.exec_command(f"kubectl get all -n {namespace} -o name")
            if exists_resource:
                print(f"Deleting K8S configs in namespace: {namespace}")
                command = f"kubectl delete -Rf {config_path} -n {namespace} --timeout=10s"
                self.exec_command(command)
            else:
                print(f"No resources found in: {namespace}. Skipping deletion.")
        except subprocess.CalledProcessError as e:
            print(f"Error deleting K8S configs: {e}")
            print(f"Command output: {e.output}")

    def delete_namespace(self, namespace: str):
        """Delete a specified namespace."""
        try:
            self.core_v1_api.delete_namespace(name=namespace)
            self.wait_for_namespace_deletion(namespace)
            print(f"Namespace '{namespace}' deleted successfully.")
        except ApiException as e:
            if e.status == 404:
                print(f"Namespace '{namespace}' not found.")
            else:
                print(f"Error deleting namespace '{namespace}': {e}")

    def create_namespace_if_not_exist(self, namespace: str):
        """Create a namespace if it doesn't exist."""
        try:
            self.core_v1_api.read_namespace(name=namespace)
            print(f"Namespace '{namespace}' already exists.")
        except ApiException as e:
            if e.status == 404:
                print(f"Namespace '{namespace}' not found. Creating namespace.")
                body = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
                self.core_v1_api.create_namespace(body=body)
                print(f"Namespace '{namespace}' created successfully.")
            else:
                print(f"Error checking/creating namespace '{namespace}': {e}")

    def exec_command(self, command: str, input_data=None):
        """Execute an arbitrary kubectl command."""
        if input_data is not None:
            input_data = input_data.encode("utf-8")
        try:
            out = subprocess.run(command, shell=True, check=True, capture_output=True, input=input_data)
            return out.stdout.decode("utf-8")
        except subprocess.CalledProcessError as e:
            return e.stderr.decode("utf-8")

        # if out.stderr:
        #     return out.stderr.decode("utf-8")
        # else:
        #     return out.stdout.decode("utf-8")

    def get_node_architectures(self):
        """Return a set of CPU architectures from all nodes in the cluster."""
        architectures = set()
        try:
            nodes = self.core_v1_api.list_node()
            for node in nodes.items:
                arch = node.status.node_info.architecture
                architectures.add(arch)
        except ApiException as e:
            print(f"Exception when retrieving node architectures: {e}\n")
        return architectures

    def get_node_memory_capacity(self):
        max_capacity = 0
        try:
            nodes = self.core_v1_api.list_node()
            for node in nodes.items:
                capacity = node.status.capacity.get("memory")
                capacity = self.parse_k8s_quantity(capacity) if capacity else 0
                max_capacity = max(max_capacity, capacity)
            return max_capacity
        except ApiException as e:
            print(f"Exception when retrieving node memory capacity: {e}\n")
            return {}

    def parse_k8s_quantity(self, mem_str):
        mem_str = mem_str.strip()
        unit_multipliers = {
            "Ki": 1,
            "Mi": 1024**1,
            "Gi": 1024**2,
            "Ti": 1024**3,
            "Pi": 1024**4,
            "Ei": 1024**5,
            "K": 1,
            "M": 1000**1,
            "G": 1000**2,
            "T": 1000**3,
            "P": 1000**4,
            "E": 1000**5,
        }

        import re

        match = re.match(r"^([0-9.]+)([a-zA-Z]+)?$", mem_str)
        if not match:
            raise ValueError(f"Invalid Kubernetes quantity: {mem_str}")

        number, unit = match.groups()
        number = float(number)
        multiplier = unit_multipliers.get(unit, 1)  # default to 1 if no unit
        return int(number * multiplier)

    def format_k8s_memory(self, bytes_value):
        units = ["Ki", "Mi", "Gi", "Ti", "Pi", "Ei"]
        value = bytes_value
        for unit in units:
            if value < 1024:
                return f"{round(value, 2)}{unit}"
            value /= 1024
        return f"{round(value, 2)}Ei"


# Example usage:
if __name__ == "__main__":
    kubectl = KubeCtl()
    namespace = "test-social-network"
    frontend_service = "nginx-thrift"
    user_service = "user-service"

    user_service_pod = kubectl.get_pod_name(namespace, f"app={user_service}")
    logs = kubectl.get_pod_logs(user_service_pod, namespace)
    print(logs)
