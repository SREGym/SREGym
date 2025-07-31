"""Inject faults at the application layer: Code, MongoDB, Redis, etc."""

import base64
import textwrap
import time

from kubernetes import client

from srearena.generators.fault.base import FaultInjector
from srearena.service.kubectl import KubeCtl


class ApplicationFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        self.namespace = namespace
        self.kubectl = KubeCtl()
        self.mongo_service_pod_map = {"mongodb-rate": "rate", "mongodb-geo": "geo"}

    def delete_service_pods(self, target_service_pods: list[str]):
        """Kill the corresponding service pod to enforce the fault."""
        for pod in target_service_pods:
            delete_pod_command = f"kubectl delete pod {pod} -n {self.namespace}"
            delete_result = self.kubectl.exec_command(delete_pod_command)
            print(f"Deleted service pod {pod} to enforce the fault: {delete_result}")

    ############# FAULT LIBRARY ################
    # A.1 - revoke_auth: Revoke admin privileges in MongoDB - Auth
    def inject_revoke_auth(self, microservices: list[str]):
        """Inject a fault to revoke admin privileges in MongoDB."""
        print(f"Microservices to inject: {microservices}")
        target_services = ["mongodb-rate", "mongodb-geo"]
        for service in target_services:
            if service in microservices:
                pods = self.kubectl.list_pods(self.namespace)
                # print(pods)
                target_mongo_pods = [pod.metadata.name for pod in pods.items if service in pod.metadata.name]
                print(f"Target MongoDB Pods: {target_mongo_pods}")

                # Find the corresponding service pod
                target_service_pods = [
                    pod.metadata.name
                    for pod in pods.items
                    if self.mongo_service_pod_map[service] in pod.metadata.name and "mongodb-" not in pod.metadata.name
                ]
                print(f"Target Service Pods: {target_service_pods}")

                for pod in target_mongo_pods:
                    if service == "mongodb-rate":
                        revoke_command = f"kubectl exec -it {pod} -n {self.namespace} -- /bin/bash /scripts/revoke-admin-rate-mongo.sh"
                    elif service == "mongodb-geo":
                        revoke_command = f"kubectl exec -it {pod} -n {self.namespace} -- /bin/bash /scripts/revoke-admin-geo-mongo.sh"
                    result = self.kubectl.exec_command(revoke_command)
                    print(f"Injection result for {service}: {result}")

                self.delete_service_pods(target_service_pods)
                time.sleep(3)

    def recover_revoke_auth(self, microservices: list[str]):
        target_services = ["mongodb-rate", "mongodb-geo"]
        for service in target_services:
            print(f"Microservices to recover: {microservices}")
            if service in microservices:
                pods = self.kubectl.list_pods(self.namespace)
                target_mongo_pods = [pod.metadata.name for pod in pods.items if service in pod.metadata.name]
                print(f"Target MongoDB Pods for recovery: {target_mongo_pods}")

                # Find the corresponding service pod
                target_service_pods = [
                    pod.metadata.name for pod in pods.items if self.mongo_service_pod_map[service] in pod.metadata.name
                ]
                for pod in target_mongo_pods:
                    if service == "mongodb-rate":
                        recover_command = f"kubectl exec -it {pod} -n {self.namespace} -- /bin/bash /scripts/revoke-mitigate-admin-rate-mongo.sh"
                    elif service == "mongodb-geo":
                        recover_command = f"kubectl exec -it {pod} -n {self.namespace} -- /bin/bash /scripts/revoke-mitigate-admin-geo-mongo.sh"
                    result = self.kubectl.exec_command(recover_command)
                    print(f"Recovery result for {service}: {result}")

                self.delete_service_pods(target_service_pods)

    # A.2 - storage_user_unregistered: User not registered in MongoDB - Storage/Net
    def inject_storage_user_unregistered(self, microservices: list[str]):
        """Inject a fault to create an unregistered user in MongoDB."""
        target_services = ["mongodb-rate", "mongodb-geo"]
        for service in target_services:
            if service in microservices:
                pods = self.kubectl.list_pods(self.namespace)
                target_mongo_pods = [pod.metadata.name for pod in pods.items if service in pod.metadata.name]
                print(f"Target MongoDB Pods: {target_mongo_pods}")

                target_service_pods = [
                    pod.metadata.name
                    for pod in pods.items
                    if pod.metadata.name.startswith(self.mongo_service_pod_map[service])
                ]
                for pod in target_mongo_pods:
                    revoke_command = (
                        f"kubectl exec -it {pod} -n {self.namespace} -- /bin/bash /scripts/remove-admin-mongo.sh"
                    )
                    result = self.kubectl.exec_command(revoke_command)
                    print(f"Injection result for {service}: {result}")

                self.delete_service_pods(target_service_pods)

    def recover_storage_user_unregistered(self, microservices: list[str]):
        target_services = ["mongodb-rate", "mongodb-geo"]
        for service in target_services:
            if service in microservices:
                pods = self.kubectl.list_pods(self.namespace)
                target_mongo_pods = [pod.metadata.name for pod in pods.items if service in pod.metadata.name]
                print(f"Target MongoDB Pods: {target_mongo_pods}")

                target_service_pods = [
                    pod.metadata.name
                    for pod in pods.items
                    if pod.metadata.name.startswith(self.mongo_service_pod_map[service])
                ]
                for pod in target_mongo_pods:
                    if service == "mongodb-rate":
                        revoke_command = f"kubectl exec -it {pod} -n {self.namespace} -- /bin/bash /scripts/remove-mitigate-admin-rate-mongo.sh"
                    elif service == "mongodb-geo":
                        revoke_command = f"kubectl exec -it {pod} -n {self.namespace} -- /bin/bash /scripts/remove-mitigate-admin-geo-mongo.sh"
                    result = self.kubectl.exec_command(revoke_command)
                    print(f"Recovery result for {service}: {result}")

                self.delete_service_pods(target_service_pods)

    # A.3 - misconfig_app: pull the buggy config of the application image - Misconfig
    def inject_misconfig_app(self, microservices: list[str]):
        """Inject a fault by pulling a buggy config of the application image.

        NOTE: currently only the geo microservice has a buggy image.
        """
        for service in microservices:
            # Get the deployment associated with the service
            deployment = self.kubectl.get_deployment(service, self.namespace)
            if deployment:
                # Modify the image to use the buggy image
                for container in deployment.spec.template.spec.containers:
                    if container.name == f"hotel-reserv-{service}":
                        container.image = "yinfangchen/geo:app3"
                self.kubectl.update_deployment(service, self.namespace, deployment)
                time.sleep(10)

    def recover_misconfig_app(self, microservices: list[str]):
        for service in microservices:
            deployment = self.kubectl.get_deployment(service, self.namespace)
            if deployment:
                for container in deployment.spec.template.spec.containers:
                    if container.name == f"hotel-reserv-{service}":
                        container.image = f"yinfangchen/hotelreservation:latest"
                self.kubectl.update_deployment(service, self.namespace, deployment)

    # A.4 valkey_auth_disruption: Invalidate the password in valkey so dependent services cannot work
    def inject_valkey_auth_disruption(self, target_service="cart"):
        pods = self.kubectl.list_pods(self.namespace)
        valkey_pods = [p.metadata.name for p in pods.items if "valkey-cart" in p.metadata.name]
        if not valkey_pods:
            print("[❌] No Valkey pod found!")
            return

        valkey_pod = valkey_pods[0]
        print(f"[🔐] Found Valkey pod: {valkey_pod}")
        command = f"kubectl exec -n {self.namespace} {valkey_pod} -- valkey-cli CONFIG SET requirepass 'invalid_pass'"
        result = self.kubectl.exec_command(command)
        print(f"[⚠️] Injection result: {result}")

        # Restart cartservice to force it to re-authenticate
        self.kubectl.exec_command(f"kubectl delete pod -l app.kubernetes.io/name={target_service} -n {self.namespace}")
        time.sleep(3)

    def recover_valkey_auth_disruption(self, target_service="cart"):
        pods = self.kubectl.list_pods(self.namespace)
        valkey_pods = [p.metadata.name for p in pods.items if "valkey-cart" in p.metadata.name]
        if not valkey_pods:
            print("[❌] No Valkey pod found for recovery!")
            return

        valkey_pod = valkey_pods[0]
        print(f"[🔓] Found Valkey pod: {valkey_pod}")
        command = f"kubectl exec -n {self.namespace} {valkey_pod} -- valkey-cli CONFIG SET requirepass ''"
        result = self.kubectl.exec_command(command)
        print(f"[✅] Recovery result: {result}")

        # Restart cartservice to restore normal behavior
        self.kubectl.exec_command(f"kubectl delete pod -l app.kubernetes.io/name={target_service} -n {self.namespace}")
        time.sleep(3)

    # A.5 valkey_memory disruption: Write large 10MB payloads to the valkey store making it go into OOM state
    def inject_valkey_memory_disruption(self):
        print("Injecting Valkey memory disruption via in-cluster job...")

        script = textwrap.dedent(
            """
            import redis
            import threading
            import time

            def flood_redis():
                client = redis.Redis(host='valkey-cart', port=6379)
                while True:
                    try:
                        payload = 'x' * 1000000
                        client.set(f"key_{time.time()}", payload)
                    except Exception as e:
                        print(f"Error: {e}")
                        time.sleep(1)

            threads = []
            for _ in range(10):
                t = threading.Thread(target=flood_redis)
                t.start()
                threads.append(t)

            for t in threads:
                t.join()
        """
        ).strip()

        encoded_script = base64.b64encode(script.encode()).decode()

        job_spec = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": "valkey-memory-flood",
                "namespace": self.namespace,
            },
            "spec": {
                "template": {
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "flooder",
                                "image": "python:3.10-slim",
                                "command": [
                                    "sh",
                                    "-c",
                                    f"pip install redis && python3 -c \"import base64; exec(base64.b64decode('{encoded_script}'))\"",
                                ],
                            }
                        ],
                    }
                }
            },
        }

        batch_v1 = client.BatchV1Api()
        batch_v1.create_namespaced_job(namespace=self.namespace, body=job_spec)
        print("Valkey memory flood job submitted.")

    def recover_valkey_memory_disruption(self):
        print("Cleaning up Valkey memory flood job...")
        batch_v1 = client.BatchV1Api()
        try:
            batch_v1.delete_namespaced_job(
                name="valkey-memory-flood",
                namespace=self.namespace,
                propagation_policy="Foreground",
            )
            print("Job deleted.")
        except Exception as e:
            print(f"Error deleting job: {e}")

    # A.5 incorrect_port_assignment: Update an env var to use the wrong port value
    def inject_incorrect_port_assignment(
        self, deployment_name: str, component_label: str, env_var: str, incorrect_port: str = "8082"
    ):
        """
        Patch the deployment to modify a specific environment variable (e.g., PRODUCT_CATALOG_SERVICE_ADDR)
        to an incorrect port (e.g., 8082 instead of 8080).
        """
        # Fetch current deployment
        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        container = deployment.spec.template.spec.containers[0]
        container_name = container.name
        current_env = container.env

        # Modify the target env var
        updated_env = []
        found = False
        for e in current_env:
            if e.name == env_var:
                updated_env.append(client.V1EnvVar(name=env_var, value=f"{e.value.split(':')[0]}:{incorrect_port}"))
                found = True
            else:
                updated_env.append(e)

        if not found:
            raise ValueError(f"Environment variable '{env_var}' not found in deployment '{deployment_name}'")

        # Create patch body
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "env": [{"name": var.name, "value": var.value} for var in updated_env],
                            }
                        ]
                    }
                }
            }
        }

        self.kubectl.patch_deployment(deployment_name, self.namespace, patch_body)
        print(f"Injected incorrect port assignment in {env_var} of {deployment_name}.")

    def recover_incorrect_port_assignment(self, deployment_name: str, env_var: str, correct_port: str = "8080"):
        """
        Revert the previously patched environment variable (e.g., PRODUCT_CATALOG_SERVICE_ADDR)
        to use the correct port (e.g., 8080).
        """
        # Fetch current deployment
        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        container = deployment.spec.template.spec.containers[0]
        container_name = container.name
        current_env = container.env

        # Revert the target env var
        updated_env = []
        found = False
        for e in current_env:
            if e.name == env_var:
                base_host = e.value.split(":")[0]
                updated_env.append(client.V1EnvVar(name=env_var, value=f"{base_host}:{correct_port}"))
                found = True
            else:
                updated_env.append(e)

        if not found:
            raise ValueError(f"Environment variable '{env_var}' not found in deployment '{deployment_name}'")

        # Create patch body
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "env": [{"name": var.name, "value": var.value} for var in updated_env],
                            }
                        ]
                    }
                }
            }
        }

        self.kubectl.patch_deployment(deployment_name, self.namespace, patch_body)
        print(f"Recovered {env_var} in {deployment_name} to use port {correct_port}.")

    # A.6 incorrect_image: checkout service is updated to use a bad image
    def inject_incorrect_image(self, deployment_name: str, namespace: str, bad_image: str = "app-image:latest"):
        # Get current deployment for container name
        deployment = self.kubectl.get_deployment(deployment_name, namespace)
        container_name = deployment.spec.template.spec.containers[0].name

        # Set replicas to 0 before updating image
        self.kubectl.patch_deployment(name=deployment_name, namespace=namespace, patch_body={"spec": {"replicas": 0}})

        # Patch image
        self.kubectl.patch_deployment(
            name=deployment_name,
            namespace=namespace,
            patch_body={"spec": {"template": {"spec": {"containers": [{"name": container_name, "image": bad_image}]}}}},
        )

        # Restore replicas to 1
        self.kubectl.patch_deployment(name=deployment_name, namespace=namespace, patch_body={"spec": {"replicas": 1}})

    def recover_incorrect_image(self, deployment_name: str, namespace: str, correct_image: str):
        deployment = self.kubectl.get_deployment(deployment_name, namespace)
        container_name = deployment.spec.template.spec.containers[0].name

        self.kubectl.patch_deployment(
            name=deployment_name,
            namespace=namespace,
            patch_body={
                "spec": {"template": {"spec": {"containers": [{"name": container_name, "image": correct_image}]}}}
            },
        )


if __name__ == "__main__":
    namespace = "test-hotel-reservation"
    # microservices = ["geo"]
    microservices = ["mongodb-geo"]
    # fault_type = "misconfig_app"
    fault_type = "storage_user_unregistered"
    print("Start injection/recover ...")
    injector = ApplicationFaultInjector(namespace)
    # injector._inject(fault_type, microservices)
    injector._recover(fault_type, microservices)
