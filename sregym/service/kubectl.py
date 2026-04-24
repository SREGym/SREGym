"""Interface to K8S controller service."""

import contextlib
import json
import logging
import subprocess
import time

logger = logging.getLogger("all.infra.kubectl")
logger.propagate = True
logger.setLevel(logging.DEBUG)

try:
    from kubernetes import client, config
except ModuleNotFoundError:
    logger.error("Your Kubeconfig is missing. Please set up a cluster.")
    exit(1)
import os  # noqa: E402

from kubernetes import dynamic  # noqa: E402
from kubernetes.client import api_client  # noqa: E402
from kubernetes.client.rest import ApiException  # noqa: E402
from rich.console import Console  # noqa: E402

WAIT_FOR_POD_READY_TIMEOUT = int(os.getenv("WAIT_FOR_POD_READY_TIMEOUT", "600"))


class KubeCtl:
    def __init__(self):
        """Initialize the KubeCtl object and load the Kubernetes configuration."""
        try:
            config.load_kube_config()
        except Exception:
            logger.error("Missing kubeconfig. Please set up a cluster.")
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

    def list_nodes(self):
        """Return a list of all running nodes."""
        return self.core_v1_api.list_node()

    def get_concise_deployments_info(self, namespace=None):
        """Return a concise info of a deployment."""
        cmd = f"kubectl get deployment {f'-n {namespace}' if namespace else ''} -o wide"
        result = self.exec_command(cmd)
        return result

    def get_concise_pods_info(self, namespace=None):
        """Return a concise info of a pod."""
        cmd = f"kubectl get pod {f'-n {namespace}' if namespace else ''} -o wide"
        result = self.exec_command(cmd)
        return result

    def list_deployments(self, namespace):
        """Return a list of all deployments within a specified namespace."""
        return self.apps_v1_api.list_namespaced_deployment(namespace)

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

    def get_namespace_deployment_status(self, namespace: str):
        """Return the deployment status of an app within a namespace."""
        try:
            deployed_services = self.apps_v1_api.list_namespaced_deployment(namespace)
            return len(deployed_services.items) > 0
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Namespace {namespace} doesn't exist.")
                return False
            else:
                raise e

    def get_service_deployment_status(self, service: str, namespace: str):
        """Return the deployment status of a single service within a namespace."""
        try:
            self.get_deployment(service, namespace)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            else:
                raise e

    def get_service(self, name: str, namespace: str):
        """Fetch the service configuration."""
        return client.CoreV1Api().read_namespaced_service(name=name, namespace=namespace)

    def wait_for_ready(
        self,
        namespace: str,
        service_names: str | list[str] | None = None,
        sleep: int = 2,
        max_wait: int = WAIT_FOR_POD_READY_TIMEOUT,
    ):
        """Wait for pods to be in a Ready state.

        Args:
            namespace: The namespace to check
            service_names: If provided (str or list), only wait for pods belonging to these services.
                           If None, wait for all pods in the namespace.
            sleep: Seconds between checks
            max_wait: Maximum seconds to wait
        """
        console = Console()

        # Normalize to list
        if service_names is None:
            services = []
        elif isinstance(service_names, str):
            services = [service_names]
        else:
            services = service_names

        # Build label selectors from services
        label_selectors = []
        for svc_name in services:
            svc = self.get_service(svc_name, namespace)
            selector_dict = svc.spec.selector or {}
            if not selector_dict:
                raise ValueError(f"Service '{svc_name}' has no selector defined")
            label_selectors.append(",".join(f"{k}={v}" for k, v in selector_dict.items()))

        if services:
            display_name = f"services {services}" if len(services) > 1 else f"service '{services[0]}'"
        else:
            display_name = f"namespace '{namespace}'"

        console.log(f"[bold yellow]Waiting for all pods in {display_name} to be ready...")

        with console.status("[bold green]Waiting for pods to be ready..."):
            wait = 0

            while wait < max_wait:
                try:
                    if label_selectors:
                        # Collect pods from all services
                        all_pods = []
                        for selector in label_selectors:
                            pod_list = self.core_v1_api.list_namespaced_pod(
                                namespace=namespace, label_selector=selector
                            )
                            all_pods.extend(pod_list.items)
                    else:
                        all_pods = self.list_pods(namespace).items or []

                    if all_pods:
                        ready_pods = [
                            pod
                            for pod in all_pods
                            if pod.status.container_statuses and all(cs.ready for cs in pod.status.container_statuses)
                        ]

                        if len(ready_pods) == len(all_pods):
                            console.log(f"[bold green]All pods in {display_name} are ready.")
                            return

                except Exception as e:
                    console.log(f"[red]Error checking pod statuses: {e}")

                time.sleep(sleep)
                wait += sleep

            raise Exception(
                f"[red]Timeout: Not all pods in {display_name} reached the Ready state within {max_wait} seconds."
            )

    def wait_for_namespace_deletion(self, namespace, sleep=2, max_wait=300):
        """Wait for a namespace to be fully deleted before proceeding."""

        console = Console()
        console.log("[bold yellow]Waiting for namespace deletion...")

        wait = 0

        while wait < max_wait:
            try:
                self.core_v1_api.read_namespace(name=namespace)
            except Exception:
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

        with console.status("[bold yellow]Waiting for pods to be stable..."):
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

    def delete_job(self, job_name: str = None, label: str = None, namespace: str = "default"):
        """Delete a Kubernetes Job."""
        console = Console()
        api_instance = client.BatchV1Api()
        try:
            if job_name:
                api_instance.delete_namespaced_job(
                    name=job_name, namespace=namespace, body=client.V1DeleteOptions(propagation_policy="Foreground")
                )
                console.log(f"[bold green]Job '{job_name}' deleted successfully.")
            elif label:
                # If label is provided, delete jobs by label
                jobs = api_instance.list_namespaced_job(namespace=namespace, label_selector=label)
                if jobs.items:
                    for job in jobs.items:
                        api_instance.delete_namespaced_job(
                            name=job.metadata.name,
                            namespace=namespace,
                            body=client.V1DeleteOptions(propagation_policy="Foreground"),
                        )
                        console.log(f"[bold green]Job with label '{label}' deleted successfully.")
                else:
                    console.log(f"[yellow]No jobs found with label '{label}' in namespace '{namespace}'.")
            return True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                console.log(f"[yellow]Job '{job_name}' not found in namespace '{namespace}' (already deleted)")
                return True
            else:
                console.log(f"[red]Error deleting job '{job_name}': {e}")
                return False
        except Exception as e:
            console.log(f"[red]Unexpected error deleting job '{job_name}': {e}")
            return False

    def wait_for_job_completion(
        self,
        job_name: str,
        namespace: str = "default",
        timeout: int = 600,
        api_client: "client.ApiClient | None" = None,
    ):
        """Wait for a Kubernetes Job to complete successfully within a specified timeout.

        Args:
            job_name: Name of the job to wait for.
            namespace: Kubernetes namespace.
            timeout: Maximum seconds to wait.
            api_client: Optional explicit ApiClient to use. If provided, bypasses the
                default (proxy-pointed) kubeconfig — useful for the workload oracle which
                needs to access workload-generator jobs that are hidden from the agent.
        """
        api_instance = client.BatchV1Api(api_client=api_client) if api_client else client.BatchV1Api()
        console = Console()
        start_time = time.time()

        console.log(f"[yellow]Waiting for job '{job_name}' to complete...")
        with console.status("[bold green]Waiting for job to be done..."):
            while time.time() - start_time < timeout:
                try:
                    job = api_instance.read_namespaced_job(name=job_name, namespace=namespace)

                    # Check job status conditions first (more reliable)
                    if job.status.conditions:
                        for condition in job.status.conditions:
                            if condition.type == "Complete" and condition.status == "True":
                                console.log(f"[bold green]Job '{job_name}' completed successfully!")
                                return
                            elif condition.type == "Failed" and condition.status == "True":
                                error_msg = f"Job '{job_name}' failed."
                                if condition.reason:
                                    error_msg += f"\nReason: {condition.reason}"
                                if condition.message:
                                    error_msg += f"\nMessage: {condition.message}"
                                console.log(f"[bold red]{error_msg}")
                                raise Exception(error_msg)

                    # Check numeric status as fallback
                    succeeded = job.status.succeeded or 0
                    failed = job.status.failed or 0

                    if succeeded > 0:
                        console.log(f"[bold green]Job '{job_name}' completed successfully! (succeeded: {succeeded})")
                        return
                    elif failed > 0:
                        console.log(f"[bold red]Job '{job_name}' failed! (failed: {failed})")
                        raise Exception(f"Job '{job_name}' failed.")

                    time.sleep(2)

                except client.exceptions.ApiException as e:
                    if e.status == 404:
                        console.log(f"[red]Job '{job_name}' not found!")
                        raise Exception(f"Job '{job_name}' not found in namespace '{namespace}'") from e
                    else:
                        console.log(f"[red]Error checking job status: {e}")
                        raise

            console.log(f"[bold red]Timeout waiting for job '{job_name}' to complete!")
            raise TimeoutError(f"Timeout: Job '{job_name}' did not complete within {timeout} seconds.")

    def update_deployment(self, name: str, namespace: str, deployment):
        """Update the deployment configuration."""
        return self.apps_v1_api.replace_namespaced_deployment(name, namespace, deployment)

    def patch_deployment(self, name: str, namespace: str, patch_body: dict):
        return self.apps_v1_api.patch_namespaced_deployment(name=name, namespace=namespace, body=patch_body)

    def patch_service(self, name, namespace, body):
        """Patch a Kubernetes service in a specified namespace."""
        try:
            api_response = self.core_v1_api.patch_namespaced_service(name, namespace, body)
            return api_response
        except ApiException as e:
            logger.error(f"Exception when patching service: {e}\n")
            return None

    def patch_custom_object(self, group, version, namespace, plural, name, body):
        """Patch a custom Kubernetes object (e.g., Chaos Mesh CRD)."""
        return self.custom_api.patch_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, name=name, body=body
        )

    def create_configmap(self, name, namespace, data):
        """Create or update a configmap from a dictionary of data."""
        try:
            api_response = self.update_configmap(name, namespace, data)
            return api_response
        except ApiException as e:
            if e.status == 404:
                return self.create_new_configmap(name, namespace, data)
            else:
                logger.error(f"Exception when updating configmap: {e}\n")
                logger.error(f"Exception status code: {e.status}\n")
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
            logger.error(f"Exception when creating configmap: {e}\n")
            return None

    def create_or_update_configmap(self, name: str, namespace: str, data: dict):
        """Create a configmap if it doesn't exist, or update it if it does."""
        try:
            existing_configmap = self.core_v1_api.read_namespaced_config_map(name, namespace)
            # ConfigMap exists, update it
            existing_configmap.data = data
            self.core_v1_api.replace_namespaced_config_map(name, namespace, existing_configmap)
            logger.info(f"ConfigMap '{name}' updated in namespace '{namespace}'")
        except ApiException as e:
            if e.status == 404:
                # ConfigMap doesn't exist, create it
                body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=name), data=data)
                self.core_v1_api.create_namespaced_config_map(namespace, body)
                logger.info(f"ConfigMap '{name}' created in namespace '{namespace}'")
            else:
                logger.error(f"Error creating/updating ConfigMap '{name}': {e}")

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
            logger.error(f"Exception when updating configmap: {e}\n")
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
                logger.info(f"Deleting K8S configs in namespace: {namespace}")
                command = f"kubectl delete -Rf {config_path} -n {namespace} --timeout=10s"
                self.exec_command(command)
            else:
                logger.warning(f"No resources found in: {namespace}. Skipping deletion.")
        except subprocess.CalledProcessError as e:
            logger.error(f"Error deleting K8S configs: {e}")
            logger.error(f"Command output: {e.output}")

    def delete_namespace(self, namespace: str):
        """Delete a specified namespace."""
        try:
            self.core_v1_api.delete_namespace(name=namespace)
            self.wait_for_namespace_deletion(namespace)
            logger.info(f"Namespace '{namespace}' deleted successfully.")
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Namespace '{namespace}' not found.")
            else:
                logger.error(f"Error deleting namespace '{namespace}': {e}")

    def gc_orphan_localpv_dirs(
        self,
        localpv_path: str = "/var/openebs/local",
        pod_namespace: str = "default",
        timeout: int = 180,
    ) -> dict:
        """Garbage-collect orphaned OpenEBS LocalPV hostpath directories on every node.

        OpenEBS hostpath PVs are reclaimed asynchronously by helper pods spawned by
        openebs-localpv-provisioner. If the openebs namespace is torn down (which the
        baseline reconciler does between problems), or the helper pod can't schedule
        on a tainted control-plane node, the hostpath dirs under
        ``/var/openebs/local/pvc-*`` leak. Over hundreds of problem cycles this
        fills the disk and breaks subsequent deploys (observed: node0 accumulated
        622 dirs / 15.8 GB after one benchmark run).

        This sweep computes the set of currently-live PVs and removes any
        ``pvc-*`` directory that doesn't correspond to one. It runs on every node
        — including control-plane — by launching a one-shot privileged pod with a
        host filesystem mount and toleration for all taints.

        Best-effort: failures on individual nodes are logged but don't raise.
        Returns a dict mapping node name -> number of orphan dirs removed.
        """
        results: dict[str, int] = {}

        # Snapshot live PVs first so we never delete a dir for a PV that exists.
        try:
            live_pv_names = {pv.metadata.name for pv in self.core_v1_api.list_persistent_volume().items}
        except Exception as e:
            logger.warning(f"[gc_localpv] Could not list PVs, skipping GC: {e}")
            return results

        try:
            node_names = [n.metadata.name for n in self.list_nodes().items]
        except Exception as e:
            logger.warning(f"[gc_localpv] Could not list nodes, skipping GC: {e}")
            return results

        if not node_names:
            return results

        logger.info(
            f"[gc_localpv] Sweeping {localpv_path} on {len(node_names)} node(s) "
            f"(preserving {len(live_pv_names)} live PV dir(s))"
        )

        # Pass the keep-list to the pod via a single env var. Newline-delimited
        # is grep -Fxq friendly. Empty string is fine — the loop just removes
        # everything matching pvc-*.
        keep_blob = "\n".join(sorted(live_pv_names))
        # The script intentionally uses /bin/sh + busybox-compatible constructs.
        script = (
            "set -u\n"
            f'cd "/host{localpv_path}" 2>/dev/null || {{ echo GC_REMOVED=0; exit 0; }}\n'
            "removed=0\n"
            "for d in pvc-*; do\n"
            '  [ -e "$d" ] || continue\n'
            '  if [ -z "${KEEP:-}" ] || ! printf "%s\\n" "$KEEP" | grep -Fxq "$d"; then\n'
            '    rm -rf -- "$d" && removed=$((removed+1))\n'
            "  fi\n"
            "done\n"
            'echo "GC_REMOVED=$removed"\n'
        )

        for node_name in node_names:
            try:
                count = self._run_localpv_gc_pod_on_node(
                    node_name=node_name,
                    namespace=pod_namespace,
                    script=script,
                    keep_blob=keep_blob,
                    timeout=timeout,
                )
                results[node_name] = count
                if count:
                    logger.info(f"[gc_localpv] {node_name}: removed {count} orphan dir(s)")
                else:
                    logger.debug(f"[gc_localpv] {node_name}: nothing to remove")
            except Exception as e:
                logger.warning(f"[gc_localpv] Failed on {node_name}: {e}")
                results[node_name] = -1

        return results

    def _run_localpv_gc_pod_on_node(
        self,
        node_name: str,
        namespace: str,
        script: str,
        keep_blob: str,
        timeout: int,
    ) -> int:
        """Run a one-shot privileged busybox pod on ``node_name`` to execute ``script``.

        Mounts host ``/`` at ``/host`` so the script can rm dirs under
        ``/host/var/openebs/local``. Tolerates all taints so it can land on
        control-plane nodes. Returns the integer parsed from the
        ``GC_REMOVED=<n>`` line in pod stdout.
        """
        # Pod name must be DNS-1123: lowercase, <=63 chars. Use first label of
        # the node hostname plus a short suffix.
        short_node = node_name.split(".")[0].lower().replace("_", "-")[:40]
        pod_name = f"sregym-localpv-gc-{short_node}-{int(time.time()) % 10000}"
        pod_name = pod_name[:63]

        pod_body = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=namespace,
                labels={"app": "sregym-localpv-gc"},
            ),
            spec=client.V1PodSpec(
                node_name=node_name,
                restart_policy="Never",
                tolerations=[client.V1Toleration(operator="Exists")],
                host_pid=False,
                automount_service_account_token=False,
                containers=[
                    client.V1Container(
                        name="gc",
                        image="busybox:1.36",
                        image_pull_policy="IfNotPresent",
                        command=["sh", "-c", script],
                        env=[client.V1EnvVar(name="KEEP", value=keep_blob)],
                        security_context=client.V1SecurityContext(privileged=True),
                        volume_mounts=[
                            client.V1VolumeMount(name="host", mount_path="/host"),
                        ],
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="host",
                        host_path=client.V1HostPathVolumeSource(path="/", type="Directory"),
                    )
                ],
            ),
        )

        # Best-effort delete in case a stale pod with the same name exists.
        with contextlib.suppress(ApiException):
            self.core_v1_api.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=0)

        self.core_v1_api.create_namespaced_pod(namespace=namespace, body=pod_body)

        try:
            # Wait for the pod to reach a terminal phase.
            waited = 0
            sleep_s = 2
            phase = "Pending"
            while waited < timeout:
                try:
                    pod = self.core_v1_api.read_namespaced_pod(name=pod_name, namespace=namespace)
                    phase = (pod.status.phase or "Pending") if pod.status else "Pending"
                except ApiException as e:
                    if e.status == 404:
                        # Got deleted out from under us — treat as failure.
                        raise RuntimeError(f"GC pod {pod_name} disappeared before completion") from e
                    raise
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(sleep_s)
                waited += sleep_s
            else:
                raise TimeoutError(f"GC pod {pod_name} on {node_name} did not finish within {timeout}s (phase={phase})")

            logs = ""
            try:
                logs = self.core_v1_api.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            except ApiException as e:
                logger.debug(f"[gc_localpv] Could not read logs for {pod_name}: {e}")

            if phase != "Succeeded":
                raise RuntimeError(
                    f"GC pod {pod_name} on {node_name} ended with phase={phase}; logs: {logs.strip()[:500]}"
                )

            removed = 0
            for line in (logs or "").splitlines():
                line = line.strip()
                if line.startswith("GC_REMOVED="):
                    with contextlib.suppress(ValueError):
                        removed = int(line.split("=", 1)[1])
                    break
            return removed
        finally:
            with contextlib.suppress(ApiException):
                self.core_v1_api.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=0)

    def create_namespace_if_not_exist(self, namespace: str):
        """Create a namespace if it doesn't exist."""
        try:
            self.core_v1_api.read_namespace(name=namespace)
            logger.info(f"Namespace '{namespace}' already exists when you want to create.")
        except ApiException as e:
            if e.status == 404:
                logger.info(f"Namespace '{namespace}' not found. Creating namespace.")
                body = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
                self.core_v1_api.create_namespace(body=body)
                logger.info(f"Namespace '{namespace}' created successfully.")
            else:
                logger.error(f"Error checking/creating namespace '{namespace}': {e}")

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
            logger.error(f"Exception when retrieving node architectures: {e}\n")
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
            logger.error(f"Exception when retrieving node memory capacity: {e}\n")
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

    def is_emulated_cluster(self) -> bool:
        try:
            nodes = self.core_v1_api.list_node()
            for node in nodes.items:
                provider_id = (node.spec.provider_id or "").lower()
                runtime = node.status.node_info.container_runtime_version.lower()
                kubelet = node.status.node_info.kubelet_version.lower()
                node_name = node.metadata.name.lower()

                if any(keyword in provider_id for keyword in ["kind", "k3d", "minikube"]):
                    return True
                if any(keyword in runtime for keyword in ["containerd://", "docker://"]) and "kind" in node_name:
                    return True
                if "minikube" in node_name or "k3d" in node_name:
                    return True
                if "kind" in kubelet:
                    return True

            return False
        except Exception as e:
            logger.error(f"Error detecting cluster type: {e}")
            return False

    def get_matching_replicasets(self, namespace: str, deployment_name: str) -> list[client.V1ReplicaSet]:
        apps_v1 = self.apps_v1_api
        rs_list = apps_v1.list_namespaced_replica_set(namespace)
        matching_rs = []

        for rs in rs_list.items:
            owner_refs = rs.metadata.owner_references
            if owner_refs:
                for owner in owner_refs:
                    if owner.kind == "Deployment" and owner.name == deployment_name:
                        matching_rs.append(rs)
                        break

        return matching_rs

    def delete_replicaset(self, name: str, namespace: str):
        body = client.V1DeleteOptions(propagation_policy="Foreground")
        try:
            self.apps_v1_api.delete_namespaced_replica_set(
                name=name,
                namespace=namespace,
                body=body,
            )
            logger.info(f"✅ Deleted ReplicaSet '{name}' in namespace '{namespace}'")
        except client.exceptions.ApiException as e:
            raise RuntimeError(f"Failed to delete ReplicaSet {name} in {namespace}: {e}") from e

    def apply_resource(self, manifest: dict):
        dyn_client = dynamic.DynamicClient(api_client.ApiClient())

        gvk = {
            ("v1", "ResourceQuota"): dyn_client.resources.get(api_version="v1", kind="ResourceQuota"),
            # Add more mappings here if needed in the future
        }

        key = (manifest["apiVersion"], manifest["kind"])
        if key not in gvk:
            raise ValueError(f"Unsupported resource type: {key}")

        resource = gvk[key]
        namespace = manifest["metadata"].get("namespace")

        try:
            resource.get(name=manifest["metadata"]["name"], namespace=namespace)
            # If exists, patch it
            resource.patch(body=manifest, name=manifest["metadata"]["name"], namespace=namespace)
            logger.info(f"✅ Patched existing {manifest['kind']} '{manifest['metadata']['name']}'")
        except dynamic.exceptions.NotFoundError:
            resource.create(body=manifest, namespace=namespace)
            logger.info(f"✅ Created new {manifest['kind']} '{manifest['metadata']['name']}'")

    def get_resource_quotas(self, namespace: str) -> list:
        try:
            response = self.core_v1_api.list_namespaced_resource_quota(namespace=namespace)
            return response.items
        except client.exceptions.ApiException as e:
            raise RuntimeError(f"Failed to get resource quotas in namespace '{namespace}': {e}") from e

    def delete_resource_quota(self, name: str, namespace: str):
        try:
            self.core_v1_api.delete_namespaced_resource_quota(
                name=name, namespace=namespace, body=client.V1DeleteOptions(propagation_policy="Foreground")
            )
            logger.info(f"✅ Deleted resource quota '{name}' in namespace '{namespace}'")
        except client.exceptions.ApiException as e:
            raise RuntimeError(f"❌ Failed to delete resource quota '{name}' in namespace '{namespace}': {e}") from e

    def scale_deployment(self, name: str, namespace: str, replicas: int):
        try:
            body = {"spec": {"replicas": replicas}}
            self.apps_v1_api.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
            logger.info(f"✅ Scaled deployment '{name}' in namespace '{namespace}' to {replicas} replicas.")
        except client.exceptions.ApiException as e:
            raise RuntimeError(f"❌ Failed to scale deployment '{name}' in namespace '{namespace}': {e}") from e

    def get_pod_cpu_usage(self, namespace: str):
        cmd = f"kubectl top pod -n {namespace} --no-headers"
        out = self.exec_command(cmd)
        # make the result into a dict
        result = {}
        for line in out.split("\n"):
            if line:
                pod_name, cpu, _ = line.split(None, 2)
                cpu = cpu.replace("m", "")
                result[pod_name] = cpu
        return result

    def trigger_rollout(self, deployment_name: str, namespace: str):
        self.exec_command(f"kubectl rollout restart deployment {deployment_name} -n {namespace}")

    def trigger_scale(self, deployment_name: str, namespace: str, replicas: int):
        self.exec_command(f"kubectl scale deployment {deployment_name} -n {namespace} --replicas={replicas}")


# Example usage:
if __name__ == "__main__":
    kubectl = KubeCtl()
    namespace = "social-network"
    frontend_service = "nginx-thrift"
    user_service = "user-service"

    user_service_pod = kubectl.get_pod_name(namespace, f"app={user_service}")
    logs = kubectl.get_pod_logs(user_service_pod, namespace)
    print(logs)
