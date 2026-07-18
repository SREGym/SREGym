"""Mitigation oracle for controller-owned finalizer deadlocks."""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle


class FinalizerDeadlockControllerMitigationOracle(Oracle):
    """Require the running controller to reconcile a fresh cleanup request."""

    importance = 1.0
    rollout_settle_seconds = 60
    cleanup_timeout_seconds = 45
    poll_interval_seconds = 2

    def __init__(
        self,
        problem,
        configmap_name: str,
        finalizer: str,
        controller_deployment_name: str,
    ):
        super().__init__(problem)
        self.configmap_name = configmap_name
        self.finalizer = finalizer
        self.controller_deployment_name = controller_deployment_name

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Cleanup Controller Recovery Evaluation ==")

        namespace = self.problem.namespace
        kubectl = self.problem.kubectl

        try:
            self._wait_for_rollouts(kubectl, namespace)

            configmap_deleted = self._wait_for_configmap_deleted(
                kubectl,
                namespace,
                self.configmap_name,
            )
            if configmap_deleted:
                print(f"[OK] ConfigMap `{self.configmap_name}` has been fully deleted.")
            else:
                print(f"[FAIL] ConfigMap `{self.configmap_name}` is still stuck in deletion.")

            controller_ok, controller_msg = self._check_controller_healthy(kubectl, namespace)
            print(controller_msg)

            app_ok, app_msg = self._check_app_healthy(kubectl, namespace)
            print(app_msg)

            durability_ok = False
            if configmap_deleted and controller_ok and app_ok:
                durability_ok = self._run_cleanup_request(kubectl, namespace)

            success = configmap_deleted and controller_ok and app_ok and durability_ok
        except Exception as exc:
            print(f"[FAIL] Error checking cleanup-controller recovery: {exc}")
            return {"success": False}

        if success:
            print("[PASS] The controller successfully reconciled a new cleanup request.")
        else:
            print("[FAIL] Cleanup-controller recovery is incomplete.")
        return {"success": success}

    @staticmethod
    def _desired_replicas(deployment) -> int:
        replicas = deployment.spec.replicas
        return 1 if replicas is None else replicas

    def _wait_for_rollouts(self, kubectl, namespace):
        deadline = time.monotonic() + self.rollout_settle_seconds
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = True
            for deployment in deployments.items:
                desired = self._desired_replicas(deployment)
                status = deployment.status
                generation = deployment.metadata.generation or 0
                if (
                    desired < 1
                    or (status.observed_generation or 0) < generation
                    or (status.updated_replicas or 0) != desired
                    or (status.ready_replicas or 0) != desired
                    or (status.available_replicas or 0) != desired
                    or (status.unavailable_replicas or 0) != 0
                ):
                    all_settled = False
                    break
            if all_settled:
                return
            time.sleep(self.poll_interval_seconds)
        print("Timed out waiting for deployments to settle; evaluating current state")

    def _wait_for_configmap_deleted(self, kubectl, namespace: str, name: str) -> bool:
        deadline = time.monotonic() + self.cleanup_timeout_seconds
        while True:
            try:
                kubectl.core_v1_api.read_namespaced_config_map(
                    name,
                    namespace,
                    _request_timeout=10,
                )
            except ApiException as exc:
                if exc.status == 404:
                    return True
                raise

            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_interval_seconds)

    def _check_controller_healthy(self, kubectl, namespace) -> tuple[bool, str]:
        try:
            deployment = kubectl.apps_v1_api.read_namespaced_deployment(
                self.controller_deployment_name,
                namespace,
                _request_timeout=10,
            )
        except ApiException as exc:
            if exc.status == 404:
                return False, f"[FAIL] Controller Deployment `{self.controller_deployment_name}` was deleted."
            return False, f"[FAIL] Could not read controller Deployment: {exc}"

        desired = self._desired_replicas(deployment)
        if desired < 1:
            return False, "[FAIL] Controller Deployment is scaled to zero."

        status = deployment.status
        generation = deployment.metadata.generation or 0
        healthy = (
            (status.observed_generation or 0) >= generation
            and (status.updated_replicas or 0) == desired
            and (status.ready_replicas or 0) == desired
            and (status.available_replicas or 0) == desired
            and (status.unavailable_replicas or 0) == 0
        )
        if not healthy:
            return False, (
                f"[FAIL] Controller Deployment is not fully rolled out ({status.ready_replicas or 0}/{desired} ready)."
            )
        return True, f"[OK] Controller Deployment is healthy ({desired}/{desired} ready)."

    def _run_cleanup_request(self, kubectl, namespace: str) -> bool:
        core_v1 = kubectl.core_v1_api
        request_name = f"reservation-cleanup-request-{time.time_ns()}"[:63]
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=request_name,
                namespace=namespace,
                finalizers=[self.finalizer],
                labels={
                    "app.kubernetes.io/component": "reservation-cleanup",
                    "app.kubernetes.io/managed-by": self.controller_deployment_name,
                },
            ),
            data={"cleanup-token": "pending", "source": "reservation-maintenance"},
        )

        created = False
        try:
            core_v1.create_namespaced_config_map(
                namespace=namespace,
                body=body,
                _request_timeout=10,
            )
            created = True
            core_v1.delete_namespaced_config_map(
                name=request_name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
                _request_timeout=10,
            )
            print(f"Created cleanup request `{request_name}` and requested deletion.")

            if self._wait_for_configmap_deleted(kubectl, namespace, request_name):
                print(f"[OK] Controller completed cleanup request `{request_name}`.")
                return True

            print(f"[FAIL] Cleanup request `{request_name}` remained stuck in deletion.")
            return False
        except ApiException as exc:
            print(f"[FAIL] Could not exercise cleanup controller: {exc}")
            return False
        finally:
            if created:
                self._remove_cleanup_request(core_v1, namespace, request_name)

    @staticmethod
    def _remove_cleanup_request(core_v1, namespace: str, name: str):
        try:
            core_v1.patch_namespaced_config_map(
                name=name,
                namespace=namespace,
                body={"metadata": {"finalizers": None}},
                _request_timeout=10,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

        with contextlib.suppress(ApiException):
            core_v1.delete_namespaced_config_map(
                name=name,
                namespace=namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
                _request_timeout=10,
            )

    def _check_app_healthy(self, kubectl, namespace) -> tuple[bool, str]:
        try:
            pods = kubectl.list_pods(namespace).items
        except Exception as exc:
            return False, f"[FAIL] Could not list pods in `{namespace}`: {exc}"

        app_pods = [
            pod
            for pod in pods
            if pod.metadata.deletion_timestamp is None
            and not pod.metadata.name.startswith(f"{self.controller_deployment_name}-")
        ]
        if not app_pods:
            return False, "[FAIL] No application pods found."

        for pod in app_pods:
            if pod.status.phase != "Running":
                return False, f"[FAIL] Pod `{pod.metadata.name}` is in phase `{pod.status.phase}`."
            for status in pod.status.container_statuses or []:
                if not status.ready:
                    return False, f"[FAIL] Container `{status.name}` in pod `{pod.metadata.name}` is not ready."

        return True, "[OK] Application pods are healthy."
