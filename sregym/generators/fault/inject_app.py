"""Inject faults at the application layer: Code, MongoDB, Redis, etc."""

import base64
import datetime
import shlex
import textwrap
import time

from kubernetes import client

from sregym.generators.fault.base import FaultInjector
from sregym.generators.images import HOTEL_GEO_MISCONFIG_IMAGE
from sregym.service.apps.hotel_reservation import HOTEL_RESERVATION_APPLICATION_IMAGE
from sregym.service.kafka_health import KafkaHealthCheck, broker_memory_failure
from sregym.service.kubectl import KubeCtl
from sregym.service.runtime_images import KAFKA_CLIENT_IMAGE, REDIS_CLIENT_IMAGE

FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE = HOTEL_RESERVATION_APPLICATION_IMAGE
KAFKA_OOM_TIMEOUT_SECONDS = 600
KAFKA_OOM_POLL_SECONDS = 5


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
                target_mongo_pods = [pod.metadata.name for pod in pods.items if service in pod.metadata.name]
                print(f"Target MongoDB Pods: {target_mongo_pods}")

                # Find the corresponding service pod
                target_service_pods = [
                    pod.metadata.name
                    for pod in pods.items
                    if self.mongo_service_pod_map[service] in pod.metadata.name and "mongodb-" not in pod.metadata.name
                ]
                print(f"Target Service Pods: {target_service_pods}")

                script = self._read_fault_script(
                    "revoke-admin-rate-mongo.sh" if service == "mongodb-rate" else "revoke-admin-geo-mongo.sh"
                )
                for pod in target_mongo_pods:
                    result = self._exec_script_in_pod(pod, script)
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

                script = self._read_fault_script(
                    "revoke-mitigate-admin-rate-mongo.sh"
                    if service == "mongodb-rate"
                    else "revoke-mitigate-admin-geo-mongo.sh"
                )
                for pod in target_mongo_pods:
                    result = self._exec_script_in_pod(pod, script)
                    print(f"Recovery result for {service}: {result}")

                self.delete_service_pods(target_service_pods)

    # A.2 - storage_user_unregistered: User not registered in MongoDB - Storage/Net
    def inject_storage_user_unregistered(self, microservices: list[str]):
        """Inject a fault to remove the admin user from MongoDB."""
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

                script = self._read_fault_script("remove-admin-mongo.sh")
                for pod in target_mongo_pods:
                    result = self._exec_script_in_pod(pod, script)
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

                script = self._read_fault_script(
                    "remove-mitigate-admin-rate-mongo.sh"
                    if service == "mongodb-rate"
                    else "remove-mitigate-admin-geo-mongo.sh"
                )
                for pod in target_mongo_pods:
                    result = self._exec_script_in_pod(pod, script)
                    print(f"Recovery result for {service}: {result}")

                self.delete_service_pods(target_service_pods)

    def _read_fault_script(self, filename: str) -> str:
        """Read a fault script from the scripts directory."""
        from sregym.paths import FAULT_SCRIPTS

        script_path = FAULT_SCRIPTS / filename
        with open(script_path) as f:
            return f.read()

    def _exec_script_in_pod(self, pod: str, script: str) -> str:
        """Execute a script inside a pod by piping it via stdin."""
        command = f"kubectl exec -i {pod} -n {self.namespace} -- /bin/bash"
        return self.kubectl.exec_command(command, input_data=script)

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
                        container.image = HOTEL_GEO_MISCONFIG_IMAGE
                self.kubectl.update_deployment(service, self.namespace, deployment)
                time.sleep(10)

    def recover_misconfig_app(self, microservices: list[str]):
        for service in microservices:
            deployment = self.kubectl.get_deployment(service, self.namespace)
            if deployment:
                for container in deployment.spec.template.spec.containers:
                    if container.name == f"hotel-reserv-{service}":
                        container.image = HOTEL_RESERVATION_APPLICATION_IMAGE
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
        command = (
            f"kubectl exec -n {self.namespace} {valkey_pod} -- "
            "env VALKEYCLI_AUTH=invalid_pass valkey-cli CONFIG SET requirepass ''"
        )
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
                                "image": REDIS_CLIENT_IMAGE,
                                "command": [
                                    "python3",
                                    "-c",
                                    f"import base64; exec(base64.b64decode('{encoded_script}'))",
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
            raise ValueError(f"Environment variable '{env_var}' not found in deployment '{deployment_name}'")

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
            f"Overrode environment variable '{env_var}' in deployment '{deployment_name}' with value '{wrong_value}'."
        )

    def recover_env_value_override(self, deployment_name: str, env_var: str, correct_value: str):
        """Restore an overridden env var to its correct value."""
        # Same shape as inject_env_value_override, but with the correct value.
        self.inject_env_value_override(deployment_name, env_var, correct_value)
        print(f"Restored environment variable '{env_var}' in deployment '{deployment_name}' to '{correct_value}'.")

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
        self.kubectl.exec_command(f"kubectl rollout restart deployment/{deployment_name} -n {self.namespace}")
        print(f"Mounted ConfigMap '{cm_name}' key '{basename}' over '{source_path}' in deployment '{deployment_name}'.")
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

        pod_spec.volumes = [v for v in (pod_spec.volumes or []) if v.name != volume_name]
        container.volume_mounts = [m for m in (container.volume_mounts or []) if m.name != volume_name]
        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)

        # Best-effort delete of the ConfigMap.
        try:
            self.kubectl.exec_command(f"kubectl delete configmap {cm_name} -n {self.namespace}")
        except Exception as e:
            print(f"Warning: failed to delete ConfigMap {cm_name}: {e}")
        print(f"Removed source override for '{source_path}' from deployment '{deployment_name}'.")

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
        set_cmd = f'kubectl exec -n {self.namespace} {pg_pod} -- psql -U {pg_superuser} -d {pg_db} -At -c "{set_sql}"'
        set_out = self.kubectl.exec_command(set_cmd).strip()
        print(f"setval({sequence!r}, {value}) -> {set_out}")

        verify_cmd = (
            f"kubectl exec -n {self.namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -At "
            f'-c "SELECT last_value FROM {sequence};"'
        )
        verify_out = self.kubectl.exec_command(verify_cmd).strip()
        try:
            live = int(verify_out.splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(
                f"Could not read last_value for sequence {sequence}; psql returned: {verify_out!r}"
            ) from e
        if live != int(value):
            raise RuntimeError(f"setval did not take effect: {sequence} last_value is {live}, expected {value}.")

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
        alter_cmd = f'kubectl exec -n {self.namespace} {pg_pod} -- psql -U {pg_superuser} -d {pg_db} -c "{alter_sql}"'
        alter_out = self.kubectl.exec_command(alter_cmd)
        print(f"ALTER ROLE {role} CONNECTION LIMIT {limit} -> {alter_out.strip()}")

        verify_sql = f"SELECT rolconnlimit FROM pg_roles WHERE rolname='{role}';"
        verify_cmd = (
            f'kubectl exec -n {self.namespace} {pg_pod} -- psql -U {pg_superuser} -d {pg_db} -At -c "{verify_sql}"'
        )
        verify_out = self.kubectl.exec_command(verify_cmd).strip()
        try:
            live = int(verify_out.splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError(f"Could not read rolconnlimit for {role}; psql returned: {verify_out!r}") from e
        if live != int(limit):
            raise RuntimeError(
                f"ALTER ROLE did not take effect: {role} rolconnlimit is {live}, expected {limit}. "
                f"Full output: {verify_out!r}"
            )

    @staticmethod
    def _kafka_oom_events(pods) -> set[tuple]:
        events = set()
        for pod in pods:
            for container in pod.status.container_statuses or []:
                if container.name != "kafka":
                    continue
                for state in (container.state, container.last_state):
                    terminated = state.terminated if state else None
                    if terminated and terminated.reason == "OOMKilled":
                        events.add((pod.metadata.uid, terminated.container_id, terminated.finished_at))
        return events

    def inject_kafka_producer_leak(self, deployment_name: str = "checkout") -> list:
        with KafkaHealthCheck(self.kubectl, self.namespace) as probe:
            if not probe.wait_until_available():
                raise RuntimeError("Kafka cannot publish and read a record before injection")
            return self._inject_kafka_producers(deployment_name, probe)

    def _inject_kafka_producers(self, deployment_name: str, probe: KafkaHealthCheck) -> list:
        limits = [None, None]
        started = datetime.datetime.now(datetime.UTC)

        kafka_dep = self.kubectl.get_deployment("kafka", self.namespace)
        previous_ooms = self._kafka_oom_events(self.kubectl.get_deployment_pods(kafka_dep, self.namespace))
        for c in kafka_dep.spec.template.spec.containers:
            if "kafka" in c.name:
                c.env.append(client.V1EnvVar(name="KAFKA_MESSAGE_MAX_BYTES", value="20971520"))

                for e in c.env:
                    if e.name == "KAFKA_HEAP_OPTS":
                        limits[0] = e.value
                        break

                limits[1] = c.resources.limits.get("memory") if c.resources and c.resources.limits else None
                break

        self.kubectl.update_deployment("kafka", self.namespace, kafka_dep)

        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)

        script = textwrap.dedent(
            """
            from confluent_kafka import Producer
            import threading
            import os

            def task(thread_id: int):
                payload_size = int(os.environ.get('PAYLOAD_SIZE_BYTES', '15728640'))
                payload = os.urandom(payload_size)

                conf = {
                    'bootstrap.servers': 'kafka:9092',
                    'message.max.bytes': payload_size + 1000,
                    'queue.buffering.max.kbytes': (payload_size * 2) // 1024,
                    'enable.idempotence': 'true',
                }

                while True:
                    try:
                        producer = Producer(conf)
                        # This binary stream must not reach the application's
                        # protobuf consumers on the real orders topic.
                        producer.produce('order-events', payload)
                        # Keep the producer alive until delivery finishes. poll(0)
                        # destroys it with most messages still queued locally.
                        producer.flush(10)
                    except BufferError:
                        producer.poll(0.1)

            threads = []
            for i in range(20):
                t = threading.Thread(target=task, args=(i,))
                t.start()
                threads.append(t)

            for t in threads:
                t.join()
            """
        ).strip()

        encoded = base64.b64encode(script.encode()).decode()

        producer = client.V1Container(
            name="order-creator",
            image=KAFKA_CLIENT_IMAGE,
            command=[
                "python3",
                "-u",
                "-c",
                f"import base64; exec(base64.b64decode('{encoded}'))",
            ],
        )

        deployment.spec.template.spec.containers.append(producer)

        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)

        # Require fresh memory failure evidence and failed Kafka delivery.
        deadline = time.monotonic() + KAFKA_OOM_TIMEOUT_SECONDS
        while True:
            pods = self.kubectl.get_deployment_pods(kafka_dep, self.namespace)
            memory_failure = bool(self._kafka_oom_events(pods) - previous_ooms) or broker_memory_failure(
                self.kubectl, self.namespace, started
            )
            if memory_failure and not probe.check():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Kafka did not develop a new memory-related serving failure within {KAFKA_OOM_TIMEOUT_SECONDS} seconds"
                )
            time.sleep(min(KAFKA_OOM_POLL_SECONDS, remaining))

        print(f"Injected sidecar container 'order-creator' in '{deployment_name}'")

        return limits

    def recover_kafka_producer_leak(self, deployment_name: str = "checkout"):
        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        replicas = deployment.spec.replicas
        deployment.spec.replicas = 0
        deployment.spec.template.spec.containers = [
            c for c in deployment.spec.template.spec.containers if c.name != "order-creator"
        ]
        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)
        try:
            # Scaling down prevents the old ReplicaSet from replacing producers
            # while checkout's replacement is waiting for the broken broker.
            selector = deployment.spec.selector.match_labels
            deadline = time.monotonic() + 120
            while any(
                all((pod.metadata.labels or {}).get(key) == value for key, value in selector.items())
                and any(c.name == "order-creator" for c in pod.spec.containers)
                for pod in self.kubectl.list_pods(self.namespace).items
            ):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Producer containers did not terminate within 120 seconds")
                time.sleep(2)

            kafka_dep = self.kubectl.get_deployment("kafka", self.namespace)
            for container in kafka_dep.spec.template.spec.containers:
                if container.name == "kafka":
                    container.env = [e for e in container.env or [] if e.name != "KAFKA_MESSAGE_MAX_BYTES"]
            # Combine config restoration and restart into one rollout. A second
            # rollout can replace a broker that just became Ready again.
            metadata = kafka_dep.spec.template.metadata
            metadata.annotations = dict(metadata.annotations or {})
            metadata.annotations["kubectl.kubernetes.io/restartedAt"] = datetime.datetime.now(datetime.UTC).isoformat()
            self.kubectl.update_deployment("kafka", self.namespace, kafka_dep)
            self.kubectl.exec_command_checked(
                shlex.join(
                    ["kubectl", "rollout", "status", "deployment/kafka", "-n", self.namespace, "--timeout=120s"]
                ),
                timeout=125,
            )
        finally:
            # Also restore the requested count after an API or startup error.
            deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
            deployment.spec.replicas = replicas
            self.kubectl.update_deployment(deployment_name, self.namespace, deployment)

        print(f"Removed sidecar container 'order-creator' from '{deployment_name}'")

    # A.7 feature_flag_experimental_routing: set a flag to activate a dormant error path
    def inject_feature_flag_experimental_routing(
        self,
        deployment_name: str = "frontend",
        configmap_name: str = "frontend-runtime-config",
        flag_key: str = "SEARCH_BACKEND_VERSION",
        experimental_image: str = FEATURE_FLAG_EXPERIMENTAL_ROUTING_IMAGE,
    ):
        """Set the feature flag and ensure the frontend uses the build containing
        the dormant path. When active, the path returns HTTP 500 on every hotel
        search request while the pod remains Running."""

        self.kubectl.create_or_update_configmap(
            name=configmap_name,
            namespace=self.namespace,
            data={flag_key: "true"},
        )
        print(f"ConfigMap {configmap_name} set: {flag_key}=true")

        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        for container in deployment.spec.template.spec.containers:
            if container.name == f"hotel-reserv-{deployment_name}":
                container.image = experimental_image
                if not container.env:
                    container.env = []
                container.env = [e for e in container.env if e.name != flag_key]
                container.env.append(
                    client.V1EnvVar(
                        name=flag_key,
                        value_from=client.V1EnvVarSource(
                            config_map_key_ref=client.V1ConfigMapKeySelector(
                                name=configmap_name,
                                key=flag_key,
                            )
                        ),
                    )
                )
        deployment.spec.strategy = client.V1DeploymentStrategy(type="Recreate")
        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)
        print(f"Set {deployment_name} image to {experimental_image} and set {flag_key}=true env")
        # Wait for rollout to complete so fault is deterministically live before returning
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{deployment_name} -n {self.namespace} --timeout=120s"
        )
        time.sleep(5)

    def recover_feature_flag_experimental_routing(
        self,
        deployment_name: str = "frontend",
        configmap_name: str = "frontend-runtime-config",
        flag_key: str = "SEARCH_BACKEND_VERSION",
        original_image: str | None = None,
    ):
        """Revert the flag, remove its pod environment reference, and restore the image."""

        self.kubectl.create_or_update_configmap(
            name=configmap_name,
            namespace=self.namespace,
            data={flag_key: "false"},
        )
        print(f"ConfigMap {configmap_name} reverted: {flag_key}=false")

        if original_image is None:
            raise ValueError("original_image must be provided")

        deployment = self.kubectl.get_deployment(deployment_name, self.namespace)
        for container in deployment.spec.template.spec.containers:
            if container.name == f"hotel-reserv-{deployment_name}":
                container.image = original_image
                container.env = [env for env in (container.env or []) if env.name != flag_key]

        deployment.spec.strategy = client.V1DeploymentStrategy(
            type="RollingUpdate",
            rolling_update=client.V1RollingUpdateDeployment(
                max_unavailable="25%",
                max_surge="25%",
            ),
        )
        self.kubectl.update_deployment(deployment_name, self.namespace, deployment)
        print(f"Restored {deployment_name} image to {original_image} and removed the {flag_key} env reference")
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{deployment_name} -n {self.namespace} --timeout=120s"
        )
        time.sleep(5)


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
