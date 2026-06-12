"""Mitigation oracle for controller-owned finalizer deadlocks."""

import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle

_REQUIRED_VERBS = {"patch", "update"}
_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5
_CONTROLLER_COMPLETION_ANNOTATION = "cleanup-controller.platform.io/finalizer-cleanup-completed"


class FinalizerDeadlockControllerMitigationOracle(Oracle):
    """Require the RBAC root cause to be fixed, not just the finalizer removed."""

    importance = 1.0

    def __init__(
        self,
        problem,
        configmap_name: str,
        finalizer: str,
        clusterrole_name: str,
        controller_deployment_name: str,
    ):
        super().__init__(problem)
        self.configmap_name = configmap_name
        self.finalizer = finalizer
        self.clusterrole_name = clusterrole_name
        self.controller_deployment_name = controller_deployment_name
        self.rbac_v1 = client.RbacAuthorizationV1Api()

    def evaluate(self) -> dict:
        print("== Finalizer Controller Deadlock Mitigation Evaluation ==")

        namespace = self.problem.namespace
        kubectl = self.problem.kubectl

        self._wait_for_rollouts(kubectl, namespace)

        clusterrole_ok, clusterrole_msg = self._check_clusterrole()
        print(clusterrole_msg)

        configmap_deleted, configmap_msg = self._check_configmap_deleted(kubectl, namespace)
        print(configmap_msg)

        if configmap_deleted and not clusterrole_ok:
            print(
                "[FAIL] Shortcut detected: the ConfigMap is gone but the controller ClusterRole "
                f"`{self.clusterrole_name}` is still missing patch/update permissions. The valid mitigation "
                "is to restore controller RBAC and let the controller remove the finalizer."
            )
            return {"success": False}

        controller_ok, controller_msg = self._check_controller_healthy(kubectl, namespace)
        print(controller_msg)

        controller_completed, controller_completed_msg = self._check_controller_completed_cleanup(kubectl, namespace)
        print(controller_completed_msg)

        app_ok, app_msg = self._check_app_healthy(kubectl, namespace)
        print(app_msg)

        success = clusterrole_ok and configmap_deleted and controller_ok and controller_completed and app_ok
        if success:
            print("[PASS] ClusterRole restored, ConfigMap deleted, controller healthy, application healthy.")
        else:
            print("[FAIL] One or more mitigation conditions failed.")
        return {"success": success}

    def _wait_for_rollouts(self, kubectl, namespace):
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = True
            for dep in deployments.items:
                desired = dep.spec.replicas or 1
                status = dep.status
                if (
                    (status.updated_replicas or 0) < desired
                    or (status.ready_replicas or 0) < desired
                    or (status.unavailable_replicas or 0) > 0
                ):
                    all_settled = False
                    break
            if all_settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print("Timed out waiting for deployments to settle; evaluating current state")

    def _check_clusterrole(self) -> tuple[bool, str]:
        try:
            role = self.rbac_v1.read_cluster_role(self.clusterrole_name, _request_timeout=10)
        except ApiException as e:
            if e.status == 404:
                return False, f"[FAIL] ClusterRole `{self.clusterrole_name}` is missing."
            return False, f"[FAIL] Could not read ClusterRole `{self.clusterrole_name}`: {e}"

        granted_on_configmaps = set()
        granted_on_finalizers = set()
        for rule in role.rules or []:
            resources = set(rule.resources or [])
            verbs = set(rule.verbs or [])
            if "*" in verbs:
                verbs.update(_REQUIRED_VERBS)
            if "configmaps" in resources or "*" in resources:
                granted_on_configmaps.update(verbs)
            if "configmaps/finalizers" in resources or "*" in resources:
                granted_on_finalizers.update(verbs)

        missing_configmaps = _REQUIRED_VERBS - granted_on_configmaps
        missing_finalizers = _REQUIRED_VERBS - granted_on_finalizers
        if missing_configmaps or missing_finalizers:
            return (
                False,
                f"[FAIL] ClusterRole `{self.clusterrole_name}` is not restored. Missing configmaps verbs: {sorted(missing_configmaps)}; "
                f"missing configmaps/finalizers verbs: {sorted(missing_finalizers)}.",
            )

        return (
            True,
            f"[OK] ClusterRole `{self.clusterrole_name}` grants patch/update on configmaps and finalizers.",
        )

    def _check_configmap_deleted(self, kubectl, namespace) -> tuple[bool, str]:
        try:
            cm = kubectl.core_v1_api.read_namespaced_config_map(
                self.configmap_name,
                namespace,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status == 404:
                return True, f"[OK] ConfigMap `{self.configmap_name}` has been fully deleted."
            return False, f"[FAIL] Could not read ConfigMap `{self.configmap_name}`: {e}"

        finalizers = cm.metadata.finalizers or []
        if cm.metadata.deletion_timestamp and self.finalizer in finalizers:
            return (
                False,
                f"[FAIL] ConfigMap `{self.configmap_name}` is still Terminating with finalizer `{self.finalizer}`.",
            )
        return False, f"[FAIL] ConfigMap `{self.configmap_name}` still exists."

    def _check_controller_healthy(self, kubectl, namespace) -> tuple[bool, str]:
        try:
            dep = kubectl.apps_v1_api.read_namespaced_deployment(
                self.controller_deployment_name,
                namespace,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status == 404:
                return False, f"[FAIL] Controller Deployment `{self.controller_deployment_name}` was deleted."
            return False, f"[FAIL] Could not read controller Deployment: {e}"

        desired = dep.spec.replicas or 1
        ready = dep.status.ready_replicas or 0
        if ready < desired:
            return False, f"[FAIL] Controller Deployment has {ready}/{desired} ready replicas."
        return True, f"[OK] Controller Deployment is healthy ({ready}/{desired} ready)."

    def _check_controller_completed_cleanup(self, kubectl, namespace) -> tuple[bool, str]:
        try:
            dep = kubectl.apps_v1_api.read_namespaced_deployment(
                self.controller_deployment_name,
                namespace,
                _request_timeout=10,
            )
        except ApiException as e:
            return False, f"[FAIL] Could not read controller cleanup annotation: {e}"

        annotations = dep.metadata.annotations or {}
        if _CONTROLLER_COMPLETION_ANNOTATION not in annotations:
            return (
                False,
                "[FAIL] Controller has not recorded finalizer cleanup completion. This rejects direct finalizer "
                "patches that bypass the controller reconcile loop.",
            )
        return True, "[OK] Controller recorded finalizer cleanup completion."

    def _check_app_healthy(self, kubectl, namespace) -> tuple[bool, str]:
        try:
            pods = kubectl.list_pods(namespace).items
        except Exception as e:
            return False, f"[FAIL] Could not list pods in `{namespace}`: {e}"

        app_pods = [pod for pod in pods if not pod.metadata.name.startswith(f"{self.controller_deployment_name}-")]
        if not app_pods:
            return False, "[FAIL] No application pods found."

        for pod in app_pods:
            if pod.status.phase != "Running":
                return False, f"[FAIL] Pod `{pod.metadata.name}` is in phase `{pod.status.phase}`."
            for status in pod.status.container_statuses or []:
                if not status.ready:
                    return False, f"[FAIL] Container `{status.name}` in pod `{pod.metadata.name}` is not ready."

        return True, "[OK] Application pods are healthy."
