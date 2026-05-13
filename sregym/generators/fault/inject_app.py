"""Inject faults at the application layer: Code, MongoDB, Redis, etc."""

import base64
import textwrap
import time

from kubernetes import client

from sregym.generators.fault.base import FaultInjector
from sregym.service.kubectl import KubeCtl


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
                        container.image = "yinfangchen/hotelreservation:latest"
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

    def inject_missing_env_variable(self, deployment_name: str, env_var: str):
        """
        Patch the deployment to delete a specific environment variable.
        """
        # Fetch current deployment
        try:
            deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
            container = deployment.spec.template.spec.containers[0]
            current_env = container.env
        except Exception as e:
            raise ValueError(f"Failed to get deployment '{deployment_name}': {e}") from e

        # Remove the target env var
        updated_env = []
        found = False
        for e in current_env:
            if e.name == env_var:
                found = True
                # Skip this environment variable (delete it)
                continue
            else:
                updated_env.append(e)

        if not found:
            raise ValueError(f"Environment variable '{env_var}' not found in deployment '{deployment_name}'")

        # Update the container's env list
        container.env = updated_env

        # Use update_deployment instead of patch_deployment
        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)
        print(f"Deleted environment variable '{env_var}' from deployment '{deployment_name}'.")

    def recover_missing_env_variable(self, deployment_name: str, env_var: str, env_value: str):
        """
        Restore the previously deleted environment variable.
        """
        # Fetch current deployment
        try:
            deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
            container = deployment.spec.template.spec.containers[0]
            container_name = container.name
            current_env = container.env
        except Exception as e:
            raise ValueError(f"Failed to get deployment '{deployment_name}': {e}") from e

        # Check if env var already exists
        for e in current_env:
            if e.name == env_var:
                print(f"Environment variable '{env_var}' already exists in deployment '{deployment_name}'.")
                return

        # Add the environment variable back
        updated_env = list(current_env)
        updated_env.append(client.V1EnvVar(name=env_var, value=env_value))

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
        print(f"Restored environment variable '{env_var}' with value '{env_value}' to deployment '{deployment_name}'.")

    def inject_env_value_override(self, deployment_name: str, env_var: str, wrong_value: str):
        """Override an existing env var's value with a wrong value (e.g. wrong unit)."""
        try:
            deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
            container = deployment.spec.template.spec.containers[0]
            container_name = container.name
            current_env = container.env or []
        except Exception as e:
            raise ValueError(f"Failed to get deployment '{deployment_name}': {e}") from e

        updated_env = []
        found = False
        for e in current_env:
            if e.name == env_var:
                updated_env.append(client.V1EnvVar(name=env_var, value=wrong_value))
                found = True
            else:
                updated_env.append(e)

        if not found:
            raise ValueError(
                f"Environment variable '{env_var}' not found in deployment '{deployment_name}'"
            )

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "env": [{"name": v.name, "value": v.value} for v in updated_env],
                            }
                        ]
                    }
                }
            }
        }
        self.kubectl.patch_deployment(deployment_name, self.namespace, patch_body)
        print(
            f"Overrode environment variable '{env_var}' in deployment '{deployment_name}' "
            f"with value '{wrong_value}'."
        )

    def recover_env_value_override(self, deployment_name: str, env_var: str, correct_value: str):
        """Restore an overridden env var to its correct value."""
        # Same shape as inject_env_value_override, but with the correct value.
        self.inject_env_value_override(deployment_name, env_var, correct_value)
        print(
            f"Restored environment variable '{env_var}' in deployment '{deployment_name}' "
            f"to '{correct_value}'."
        )

    def inject_source_file_override(
        self,
        deployment_name: str,
        source_path: str,
        replacement_content: str,
        configmap_name: str | None = None,
    ) -> str:
        """Overlay a single file inside a running container with a patched
        version via a ConfigMap subPath mount.

        This is the code-change variant of fault injection — the replacement
        content is written into a ConfigMap, the deployment is patched to
        mount that key at `source_path` (subPath so only this one file is
        replaced, not the whole directory), and the pod restarts into the
        patched state. Recovery removes the mount and the ConfigMap, so the
        next pod starts reading the image's original file.

        Returns the name of the ConfigMap (either the provided one or a
        derived `<deployment>-src-override`).
        """
        cm_name = configmap_name or f"{deployment_name}-src-override"
        basename = source_path.rsplit("/", 1)[-1]
        volume_name = f"{cm_name}-vol"

        # Create (or replace) the ConfigMap holding the patched file.
        self.kubectl.create_or_update_configmap(
            cm_name,
            self.namespace,
            {basename: replacement_content},
        )

        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        pod_spec = deployment.spec.template.spec
        container = pod_spec.containers[0]

        # Build the new volume + volumeMount, preserving existing ones.
        existing_volumes = list(pod_spec.volumes or [])
        if not any(v.name == volume_name for v in existing_volumes):
            existing_volumes.append(
                client.V1Volume(
                    name=volume_name,
                    config_map=client.V1ConfigMapVolumeSource(name=cm_name),
                )
            )
        existing_mounts = list(container.volume_mounts or [])
        if not any(m.name == volume_name for m in existing_mounts):
            existing_mounts.append(
                client.V1VolumeMount(
                    name=volume_name,
                    mount_path=source_path,
                    sub_path=basename,
                    read_only=True,
                )
            )

        pod_spec.volumes = existing_volumes
        container.volume_mounts = existing_mounts
        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)
        # ConfigMap subPath mounts snapshot the value at pod-start time and do
        # *not* hot-reload when the ConfigMap changes, so force a rollout even
        # when the Deployment spec itself wasn't modified on this call.
        self.kubectl.exec_command(
            f"kubectl rollout restart deployment/{deployment_name} -n {self.namespace}"
        )
        print(
            f"Mounted ConfigMap '{cm_name}' key '{basename}' over '{source_path}' "
            f"in deployment '{deployment_name}'."
        )
        return cm_name

    def recover_source_file_override(
        self,
        deployment_name: str,
        source_path: str,
        configmap_name: str | None = None,
    ):
        """Remove the source-file overlay added by inject_source_file_override."""
        cm_name = configmap_name or f"{deployment_name}-src-override"
        volume_name = f"{cm_name}-vol"

        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        pod_spec = deployment.spec.template.spec
        container = pod_spec.containers[0]

        pod_spec.volumes = [
            v for v in (pod_spec.volumes or []) if v.name != volume_name
        ]
        container.volume_mounts = [
            m for m in (container.volume_mounts or []) if m.name != volume_name
        ]
        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)

        # Best-effort delete of the ConfigMap.
        try:
            self.kubectl.exec_command(
                f"kubectl delete configmap {cm_name} -n {self.namespace}"
            )
        except Exception as e:
            print(f"Warning: failed to delete ConfigMap {cm_name}: {e}")
        print(
            f"Removed source override for '{source_path}' from deployment "
            f"'{deployment_name}'."
        )

    def set_sequence_value(
        self,
        pg_pod: str,
        pg_superuser: str,
        pg_db: str,
        sequence: str,
        value: int,
    ):
        """Set a PostgreSQL sequence's current value via setval().

        Used to simulate a near-exhausted INT4 sequence (integer-overflow fault)
        or to recover by resetting to a known safe value. Verifies the new
        value is readable back via pg_sequences to catch silent failures.
        """
        set_sql = f"SELECT setval('{sequence}', {int(value)});"
        set_cmd = (
            f"kubectl exec -n {self.namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -At -c \"{set_sql}\""
        )
        set_out = self.kubectl.exec_command(set_cmd).strip()
        print(f"setval({sequence!r}, {value}) -> {set_out}")

        verify_cmd = (
            f"kubectl exec -n {self.namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -At "
            f"-c \"SELECT last_value FROM {sequence};\""
        )
        verify_out = self.kubectl.exec_command(verify_cmd).strip()
        try:
            live = int(verify_out.splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(
                f"Could not read last_value for sequence {sequence}; psql returned: {verify_out!r}"
            ) from e
        if live != int(value):
            raise RuntimeError(
                f"setval did not take effect: {sequence} last_value is {live}, expected {value}."
            )

    def inject_role_connection_limit(
        self,
        pg_pod: str,
        pg_superuser: str,
        pg_db: str,
        role: str,
        limit: int,
    ):
        """Set a PostgreSQL role's CONNECTION LIMIT via `kubectl exec … psql`.

        CONNECTION LIMIT is dynamic (no server restart required). Setting it to 0
        blocks all new connections for that role; -1 means unlimited (the default).

        Raises if the ALTER didn't take effect — `kubectl.exec_command` swallows
        non-zero exits and returns stderr, so the caller wouldn't otherwise
        notice a bad pod reference or auth failure.
        """
        alter_sql = f"ALTER ROLE {role} CONNECTION LIMIT {int(limit)};"
        alter_cmd = (
            f"kubectl exec -n {self.namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -c \"{alter_sql}\""
        )
        alter_out = self.kubectl.exec_command(alter_cmd)
        print(f"ALTER ROLE {role} CONNECTION LIMIT {limit} -> {alter_out.strip()}")

        verify_sql = f"SELECT rolconnlimit FROM pg_roles WHERE rolname='{role}';"
        verify_cmd = (
            f"kubectl exec -n {self.namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -At -c \"{verify_sql}\""
        )
        verify_out = self.kubectl.exec_command(verify_cmd).strip()
        try:
            live = int(verify_out.splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(
                f"Could not read rolconnlimit for {role}; psql returned: {verify_out!r}"
            ) from e
        if live != int(limit):
            raise RuntimeError(
                f"ALTER ROLE did not take effect: {role} rolconnlimit is {live}, expected {limit}. "
                f"Full output: {verify_out!r}"
            )


if __name__ == "__main__":
    namespace = "hotel-reservation"
    # microservices = ["geo"]
    microservices = ["mongodb-geo"]
    # fault_type = "misconfig_app"
    fault_type = "storage_user_unregistered"
    print("Start injection/recover ...")
    injector = ApplicationFaultInjector(namespace)
    # injector._inject(fault_type, microservices)
    injector._recover(fault_type, microservices)
