import logging
from logging import getLogger
from typing import Any

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from sregym.conductor.oracles.base import Oracle

logger = getLogger("all.sregym.diagnosis_oracle")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class DiagnosisOracle(Oracle):
    """Logic of Diagnosis Oracle"""

    # BEFORE the agent are ask to act, expect function will be called and checkpoint will be saved
    # AFTER the agent finish its run, the expected function will be called AGAIN to compare with agents answer.

    def __init__(self, problem, namespace: str):
        super().__init__(problem)
        # self.expected = expected
        self.checkpoint = None
        self.namespace = namespace

    def load_diagnosis_checkpoint(self):
        # load the checkpoint for future comparison
        self.checkpoint = self.expect()

    def compare_truth(self, expectation, reality):
        if type(expectation) == str and type(reality) == str:
            return expectation == reality  # both string, just compare the string
        elif type(expectation) == list and type(reality) == list:
            if len(expectation) != len(set(reality)):
                return False  # TODO: support fp and fn
            return all(e in set(reality) for e in expectation)
        else:
            logger.warning(
                f"Expectation and reality are not both string or list, can not compare. Expectation: {expectation}, Reality: {reality}"
            )
            return False

    def expect(self):
        raise NotImplementedError("This function should be implemented by the subclass.")

    def verify_stability(self, new_expectation):
        consistent = self.compare_truth(self.checkpoint, new_expectation)

        if not consistent:
            # just warn, do not panic
            logger.warning(
                f"Checkpoints are not consistent, old: {self.checkpoint}, new: {new_expectation}. Possibly the environment is unstable."
            )

        return consistent

    def get_resource_uid(self, resource_type: str, resource_name: str, namespace: str) -> str | None:
        """Return the UID of a live resource using the Kubernetes API."""
        try:
            try:
                config.load_incluster_config()
            except ConfigException:
                config.load_kube_config()
            if resource_type.lower() == "pod":
                api = client.CoreV1Api()
                obj = api.read_namespaced_pod(resource_name, namespace)
            elif resource_type.lower() == "service":
                api = client.CoreV1Api()
                obj = api.read_namespaced_service(resource_name, namespace)
            elif resource_type.lower() == "deployment":
                api = client.AppsV1Api()
                obj = api.read_namespaced_deployment(resource_name, namespace)
            elif resource_type.lower() == "statefulset":
                api = client.AppsV1Api()
                obj = api.read_namespaced_stateful_set(resource_name, namespace)
            elif resource_type.lower() == "persistentvolumeclaim":
                api = client.CoreV1Api()
                obj = api.read_namespaced_persistent_volume_claim(resource_name, namespace)
            elif resource_type.lower() == "persistentvolume":
                api = client.CoreV1Api()
                obj = api.read_persistent_volume(resource_name)
            elif resource_type.lower() == "configmap":
                api = client.CoreV1Api()
                obj = api.read_namespaced_config_map(resource_name, namespace)
            elif resource_type.lower() == "replicaset":
                api = client.AppsV1Api()
                obj = api.read_namespaced_replica_set(resource_name, namespace)
            elif resource_type.lower() == "memoryquota":
                api = client.CoreV1Api()
                obj = api.read_namespaced_resource_quota(resource_name, namespace)
            elif resource_type.lower() == "ingress":
                api = client.NetworkingV1Api()
                obj = api.read_namespaced_ingress(resource_name, namespace)
            elif resource_type.lower() == "job":
                api = client.BatchV1Api()
                obj = api.read_namespaced_job(resource_name, namespace)
            elif resource_type.lower() == "daemonset":
                api = client.AppsV1Api()
                obj = api.read_namespaced_daemon_set(resource_name, namespace)
            elif resource_type.lower() == "clusterrole":
                api = client.RbacAuthorizationV1Api()
                obj = api.read_cluster_role(resource_name)
            elif resource_type.lower() == "clusterrolebinding":
                api = client.RbacAuthorizationV1Api()
                obj = api.read_cluster_role_binding(resource_name)
            else:
                raise ValueError(f"Unsupported resource type: {resource_type}")

            return obj.metadata.uid

        except client.exceptions.ApiException as e:
            return f"Error retrieving UID for {resource_type}/{resource_name} in {namespace}: {e.reason}"

    def checkpoint_comparison(self, new_expectation):
        if type(self.checkpoint) == str:
            return self.checkpoint == new_expectation

    def safe_parse_solution(self, solution):
        # Normalize solution to list of strings
        if isinstance(solution, str):
            # Check if it's a comma-separated list
            # strip char before [
            if "[" in solution and "]" in solution:
                solution = solution.split("[")[1]
                # strip char after ]
                solution = solution.split("]")[0]
                if "," in solution:
                    # split by comma, strip space and quote
                    solution = [s.strip().strip("\"'") for s in solution.split(",")]
                else:
                    solution = [solution.strip().strip("\"'")]
            else:
                solution = [solution.strip().strip("\"'")]
        elif isinstance(solution, list):
            # Ensure all items are strings
            solution = [str(item) for item in solution]
        else:
            return None
        return solution

    def evaluate(self, solution) -> dict[str, Any]:
        # verify the stability of the environment
        new_expectation = self.expect()
        self.verify_stability(new_expectation)

        # load the solution
        solution = self.safe_parse_solution(solution)
        if solution is None:
            logger.warning(f"Invalid format: expected string or list of strings. Solution: {solution}")
            return {
                "success": False,
                "accuracy": 0.0,
                "is_subset": False,
            }

        # get compare the new expectation with the checkpoint
        correctness = self.compare_truth(new_expectation, solution)

        logger.info(
            f"Eval Diagnosis: new_expectation: {new_expectation}, solution: {solution} | {"✅" if correctness else "❌"}"
        )

        return {
            "success": correctness,
            "accuracy": 100.0 if correctness else 0.0,
            "is_subset": False,  # TODO: enable subset match
        }

    ####### Helper functions ######
    def only_pod_of_deployment_uid(self, deployment_name: str, namespace: str) -> tuple[str, str]:
        """Return the UID and name of the only pod of a deployment. If not only or more than one pod, Raise an error."""
        try:
            # print("find pods for deployment", deployment_name, "in namespace", namespace)
            pods_list = client.CoreV1Api().list_namespaced_pod(
                namespace=namespace, label_selector=f"app={deployment_name}"
            )
            # print("pods_list", pods_list)
            pods = pods_list.items  # V1PodList has an 'items' attribute containing the actual list

            # TODO: use more robust way to select the pod OwnerRef or understand the deployment spec.

            # fallback, use io.kompose.service label
            if len(pods) == 0:
                logger.debug("fallback to io.kompose.service label")
                pods_list = client.CoreV1Api().list_namespaced_pod(
                    namespace=namespace, label_selector=f"io.kompose.service={deployment_name}"
                )
                # print("pods_list", pods_list)
                pods = pods_list.items

            # fallback 2, use opentelemetry label to select the pod
            if len(pods) == 0:
                logger.debug("fallback to opentelemetry label")
                pods_list = client.CoreV1Api().list_namespaced_pod(
                    namespace=namespace, label_selector=f"opentelemetry.io/name={deployment_name}"
                )
                # print("pods_list", pods_list)
                pods = pods_list.items

            if len(pods) > 1:
                # print(pods)
                raise ValueError(
                    f"More than one pod found for deployment {deployment_name} in namespace {namespace}: {pods}, can not evaluate diagnosis."
                )
            if len(pods) == 0:
                # print(pods)
                raise ValueError(
                    f"No pod found for deployment {deployment_name} in namespace {namespace}, can not evaluate diagnosis."
                )
            return pods[0].metadata.uid, pods[0].metadata.name
        except Exception as e:
            raise ValueError(f"Error retrieving pod UID for deployment {deployment_name} in namespace {namespace}: {e}")

    def all_pods_of_deployment_uids(self, deployment_name: str, namespace: str) -> (list[str], list[str]):
        """Return the UIDs and names of all pods of a deployment."""
        try:
            pods_list = client.CoreV1Api().list_namespaced_pod(
                namespace=namespace, label_selector=f"app={deployment_name}"
            )
            pods = pods_list.items
            if len(pods) == 0:
                pods_list = client.CoreV1Api().list_namespaced_pod(
                    namespace=namespace, label_selector=f"io.kompose.service={deployment_name}"
                )
                pods = pods_list.items
            if len(pods) == 0:
                pods_list = client.CoreV1Api().list_namespaced_pod(
                    namespace=namespace, label_selector=f"opentelemetry.io/name={deployment_name}"
                )
                pods = pods_list.items
            return [pod.metadata.uid for pod in pods], [pod.metadata.name for pod in pods]
        except Exception as e:
            raise ValueError(f"Error retrieving pods for deployment {deployment_name} in namespace {namespace}: {e}")

    def all_pods_of_daemonset_uids(self, daemonset_name: str, namespace: str) -> (list[str], list[str]):
        """Return the UIDs and names of all pods of a daemonset."""
        try:
            pods_list = client.CoreV1Api().list_namespaced_pod(
                namespace=namespace, label_selector=f"k8s-app={daemonset_name}"
            )
            pods = pods_list.items
            if len(pods) == 0:
                pods_list = client.CoreV1Api().list_namespaced_pod(
                    namespace=namespace, label_selector=f"app={daemonset_name}"
                )
                pods = pods_list.items
            if len(pods) == 0:
                pods_list = client.CoreV1Api().list_namespaced_pod(
                    namespace=namespace, label_selector=f"io.kompose.service={daemonset_name}"
                )
                pods = pods_list.items
            if len(pods) == 0:
                pods_list = client.CoreV1Api().list_namespaced_pod(
                    namespace=namespace, label_selector=f"opentelemetry.io/name={daemonset_name}"
                )
                pods = pods_list.items
            return [pod.metadata.uid for pod in pods], [pod.metadata.name for pod in pods]
        except Exception as e:
            raise ValueError(f"Error retrieving pods for daemonset {daemonset_name} in namespace {namespace}: {e}")

    def deployment_uid(self, deployment_name: str, namespace: str) -> str:
        """Return the UID of a deployment."""
        return self.get_resource_uid("deployment", deployment_name, namespace)

    def configmap_uid(self, configmap_name: str, namespace: str) -> str:
        """Return the UID of a configmap."""
        return self.get_resource_uid("configmap", configmap_name, namespace)

    def pvc_uid(self, pvc_name: str, namespace: str) -> str:
        """Return the UID of a PVC."""
        return self.get_resource_uid("persistentvolumeclaim", pvc_name, namespace)

    def service_uid(self, service_name: str, namespace: str) -> str:
        """Return the UID of a service."""
        return self.get_resource_uid("service", service_name, namespace)

    def memoryquota_uid(self, memoryquota_name: str, namespace: str) -> str:
        """Return the UID of a memoryquota."""
        return self.get_resource_uid("memoryquota", memoryquota_name, namespace)

    def pv_uid(self, pv_name: str, namespace: str) -> str:
        """Return the UID of a PV."""
        return self.get_resource_uid("persistentvolume", pv_name, namespace)

    def ingress_uid(self, ingress_name: str, namespace: str) -> str:
        """Return the UID of an ingress."""
        return self.get_resource_uid("ingress", ingress_name, namespace)

    def networkpolicy_uid(self, networkpolicy_name: str, namespace: str) -> str:
        """Return the UID of a networkpolicy."""
        return self.get_resource_uid("networkpolicy", networkpolicy_name, namespace)

    def job_uid(self, job_name: str, namespace: str) -> str:
        """Return the UID of a job."""
        return self.get_resource_uid("job", job_name, namespace)

    def clusterrole_uid(self, clusterrole_name: str, namespace: str) -> str:
        """Return the UID of a clusterrole."""
        return self.get_resource_uid("clusterrole", clusterrole_name, namespace)

    def clusterrolebinding_uid(self, clusterrolebinding_name: str, namespace: str) -> str:
        """Return the UID of a clusterrolebinding."""
        return self.get_resource_uid("clusterrolebinding", clusterrolebinding_name, namespace)

    def owner_of_pod(self, pod_name: str, namespace: str) -> dict[str, Any] | None:
        """
        Return the top-level owner (controller) of a pod using Kubernetes Owner References.

        This method follows the owner chain to find the ultimate controller:
        - Pod → ReplicaSet → Deployment
        - Pod → StatefulSet (direct)
        - Pod → DaemonSet (direct)
        - Pod → Job → CronJob (if applicable)

        Args:
            pod_name: Name of the pod
            namespace: Namespace of the pod

        Returns:
            Dictionary with keys: 'kind', 'name', 'uid', 'api_version'
            Returns None if no owner is found or pod doesn't exist

        Example:
            {
                'kind': 'Deployment',
                'name': 'frontend',
                'uid': 'abc-123-def',
                'api_version': 'apps/v1'
            }
        """
        try:
            try:
                config.load_incluster_config()
            except ConfigException:
                config.load_kube_config()
        except Exception as e:
            raise RuntimeError(f"Failed to load kube config: {e}")

        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()
        batch_v1 = client.BatchV1Api()

        try:
            # Step 1: Get the pod
            pod = core_v1.read_namespaced_pod(pod_name, namespace)
        except client.exceptions.ApiException as e:
            print(f"[WARN] Pod '{pod_name}' not found in namespace '{namespace}': {e.reason}")
            return None

        # Step 2: Get owner references from pod
        owner_refs = pod.metadata.owner_references
        if not owner_refs:
            print(f"[INFO] Pod '{pod_name}' has no owner references (may be manually created)")
            return None

        # Step 3: Find the controller owner (controller: true)
        controller_owner = None
        for owner in owner_refs:
            if owner.controller:
                controller_owner = owner
                break

        if not controller_owner:
            print(f"[WARN] Pod '{pod_name}' has no controller owner")
            return None

        # Step 4: Handle different owner types
        owner_kind = controller_owner.kind
        owner_name = controller_owner.name
        owner_uid = controller_owner.uid
        owner_api_version = controller_owner.api_version

        print(f"[INFO] Pod '{pod_name}' is owned by {owner_kind} '{owner_name}'")

        # Step 5: If owner is ReplicaSet, continue up to find Deployment
        if owner_kind == "ReplicaSet":
            try:
                replicaset = apps_v1.read_namespaced_replica_set(owner_name, namespace)
                rs_owner_refs = replicaset.metadata.owner_references

                if rs_owner_refs:
                    for rs_owner in rs_owner_refs:
                        if rs_owner.controller and rs_owner.kind == "Deployment":
                            print(f"[INFO] ReplicaSet '{owner_name}' is owned by Deployment '{rs_owner.name}'")
                            return {
                                "kind": "Deployment",
                                "name": rs_owner.name,
                                "uid": rs_owner.uid,
                                "api_version": rs_owner.api_version,
                                "intermediate_owner": {"kind": "ReplicaSet", "name": owner_name, "uid": owner_uid},
                            }

                # If ReplicaSet has no owner, return ReplicaSet itself
                print(f"[INFO] ReplicaSet '{owner_name}' has no owner (may be manually created)")
                return {"kind": "ReplicaSet", "name": owner_name, "uid": owner_uid, "api_version": owner_api_version}
            except client.exceptions.ApiException as e:
                print(f"[WARN] ReplicaSet '{owner_name}' not found: {e.reason}")
                # Fallback: return ReplicaSet info even though we can't verify it
                return {"kind": "ReplicaSet", "name": owner_name, "uid": owner_uid, "api_version": owner_api_version}

        # Step 6: If owner is Job, check if it's owned by CronJob
        elif owner_kind == "Job":
            try:
                job = batch_v1.read_namespaced_job(owner_name, namespace)
                job_owner_refs = job.metadata.owner_references

                if job_owner_refs:
                    for job_owner in job_owner_refs:
                        if job_owner.controller and job_owner.kind == "CronJob":
                            print(f"[INFO] Job '{owner_name}' is owned by CronJob '{job_owner.name}'")
                            return {
                                "kind": "CronJob",
                                "name": job_owner.name,
                                "uid": job_owner.uid,
                                "api_version": job_owner.api_version,
                                "intermediate_owner": {"kind": "Job", "name": owner_name, "uid": owner_uid},
                            }

                # If Job has no owner, return Job itself
                return {"kind": "Job", "name": owner_name, "uid": owner_uid, "api_version": owner_api_version}
            except client.exceptions.ApiException as e:
                print(f"[WARN] Job '{owner_name}' not found: {e.reason}")
                return {"kind": "Job", "name": owner_name, "uid": owner_uid, "api_version": owner_api_version}

        # Step 7: Direct owners (StatefulSet, DaemonSet, etc.)
        else:
            return {"kind": owner_kind, "name": owner_name, "uid": owner_uid, "api_version": owner_api_version}

    def pods_of_owner(self, owner_kind: str, owner_name: str, namespace: str) -> list[dict[str, Any]]:
        """
        Find all pods owned by a specific controller (Deployment, StatefulSet, etc.).

        This is the reverse operation of owner_of_pod. It finds all pods that belong
        to a given controller by following the owner chain.

        Args:
            owner_kind: Kind of the owner (Deployment, StatefulSet, DaemonSet, etc.)
            owner_name: Name of the owner
            namespace: Namespace of the owner

        Returns:
            List of pod information dictionaries, each containing:
            - 'name': Pod name
            - 'uid': Pod UID
            - 'phase': Pod phase (Running, Pending, etc.)

        Example:
            [
                {
                    'name': 'frontend-abc123',
                    'uid': 'pod-uid-123',
                    'phase': 'Running'
                },
                ...
            ]
        """
        try:
            try:
                config.load_incluster_config()
            except ConfigException:
                config.load_kube_config()
        except Exception as e:
            raise RuntimeError(f"Failed to load kube config: {e}")

        core_v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()
        pods_info = []

        try:
            # Get all pods in the namespace
            all_pods = core_v1.list_namespaced_pod(namespace).items

            # Handle different owner types
            if owner_kind == "Deployment":
                # Find ReplicaSets owned by this Deployment
                rs_list = apps_v1.list_namespaced_replica_set(namespace).items
                matching_rs_names = set()

                for rs in rs_list:
                    rs_owner_refs = rs.metadata.owner_references
                    if rs_owner_refs:
                        for rs_owner in rs_owner_refs:
                            if rs_owner.controller and rs_owner.kind == "Deployment" and rs_owner.name == owner_name:
                                matching_rs_names.add(rs.metadata.name)

                # Find pods owned by matching ReplicaSets
                for pod in all_pods:
                    pod_owner_refs = pod.metadata.owner_references
                    if pod_owner_refs:
                        for pod_owner in pod_owner_refs:
                            if (
                                pod_owner.controller
                                and pod_owner.kind == "ReplicaSet"
                                and pod_owner.name in matching_rs_names
                            ):
                                pods_info.append(
                                    {
                                        "name": pod.metadata.name,
                                        "uid": pod.metadata.uid,
                                        "phase": pod.status.phase,
                                        "node_name": pod.spec.node_name if pod.spec.node_name else None,
                                    }
                                )

            elif owner_kind in ["StatefulSet", "DaemonSet"]:
                # Direct ownership for StatefulSet and DaemonSet
                for pod in all_pods:
                    pod_owner_refs = pod.metadata.owner_references
                    if pod_owner_refs:
                        for pod_owner in pod_owner_refs:
                            if pod_owner.controller and pod_owner.kind == owner_kind and pod_owner.name == owner_name:
                                pods_info.append(
                                    {
                                        "name": pod.metadata.name,
                                        "uid": pod.metadata.uid,
                                        "phase": pod.status.phase,
                                        "node_name": pod.spec.node_name if pod.spec.node_name else None,
                                    }
                                )

            elif owner_kind == "Job":
                # Direct ownership for Job
                for pod in all_pods:
                    pod_owner_refs = pod.metadata.owner_references
                    if pod_owner_refs:
                        for pod_owner in pod_owner_refs:
                            if pod_owner.controller and pod_owner.kind == "Job" and pod_owner.name == owner_name:
                                pods_info.append(
                                    {
                                        "name": pod.metadata.name,
                                        "uid": pod.metadata.uid,
                                        "phase": pod.status.phase,
                                        "node_name": pod.spec.node_name if pod.spec.node_name else None,
                                    }
                                )

            else:
                print(f"[WARN] Unsupported owner kind: {owner_kind}")
                return []

            print(f"[INFO] Found {len(pods_info)} pod(s) owned by {owner_kind} '{owner_name}'")
            return pods_info

        except Exception as e:
            print(f"[ERROR] Failed to find pods for {owner_kind} '{owner_name}': {e}")
            return []
