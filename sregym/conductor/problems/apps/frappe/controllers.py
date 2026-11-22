"""Frappe: controllers."""

from __future__ import annotations

import json
import time
from pathlib import Path

from kubernetes import client

from sregym.conductor.oracles.cronjob_sidecar_mitigation import CronJobSidecarBlocksCompletionMitigationOracle
from sregym.conductor.oracles.finalizer_deadlock_controller_mitigation import (
    FinalizerDeadlockControllerMitigationOracle,
)
from sregym.conductor.oracles.rolling_update_misconfiguration_mitigation import RollingUpdateMitigationOracle
from sregym.conductor.oracles.wrong_pod_selection_mitigation import WrongPodSelectionMitigationOracle
from sregym.conductor.problems import finalizer_deadlock_controller as finalizer_module
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.cronjob_sidecar_blocks_completion import CronJobSidecarBlocksCompletionHotelReservation
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.conductor.problems.finalizer_deadlock_controller import FinalizerDeadlockController
from sregym.utils.decorators import mark_fault_injected


class CronJobSidecarBlocksCompletionIA(CronJobSidecarBlocksCompletionHotelReservation):
    def __init__(self, app_name: str = "frappe"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = self.CRONJOB_NAME
        self.cronjob_name = self.CRONJOB_NAME
        configure_port(
            self,
            app_name,
            component=f"CronJob/{self.CRONJOB_NAME}",
            description=(
                f"The CronJob '{self.CRONJOB_NAME}' schedules a pod every minute whose jobTemplate has two regular "
                f"containers: a primary ('{self.PRIMARY_CONTAINER}') that performs a short archival step and exits "
                f"cleanly, and a log-shipper sidecar ('{self.SIDECAR_CONTAINER}') that never terminates. A Job is "
                "Complete only when every container in its pod terminates, so each pod stays Running after the "
                "primary exits and each schedule adds another active Job that never completes; "
                "successfulJobsHistoryLimit does not apply to active Jobs, so they accumulate. A repair must "
                "preserve archival and forwarding while letting each Job complete, using native sidecars "
                "(initContainers with restartPolicy: Always) or cooperative regular-sidecar shutdown. The "
                "accumulated active Jobs must also be cleaned up. Killing unfinished Jobs at a deadline, "
                "removing the sidecar, or deleting the CronJob are not acceptable."
            ),
            oracle_factory=CronJobSidecarBlocksCompletionMitigationOracle,
        )
        self.batch_v1 = client.BatchV1Api()
        self.core_v1 = client.CoreV1Api()


class FinalizerDeadlockControllerIA(FinalizerDeadlockController):
    def __init__(self, app_name: str = "frappe"):
        super().__init__(app=make_app(app_name))
        self.configmap_name = finalizer_module._CONFIGMAP_NAME
        self.finalizer = finalizer_module._FINALIZER
        self.controller_name = finalizer_module._CONTROLLER_NAME
        self.sa_name = finalizer_module._SA_NAME
        self.clusterrole_name = finalizer_module._CLUSTERROLE_NAME
        self.clusterrolebinding_name = finalizer_module._CLUSTERROLEBINDING_NAME
        self.faulty_service = self.configmap_name
        configure_port(
            self,
            app_name,
            component=(
                f"ClusterRole/{self.clusterrole_name} (RBAC of ServiceAccount/{self.sa_name}) "
                f"and configmap/{self.configmap_name}"
            ),
            description=(
                f"ConfigMap `{self.configmap_name}` is stuck in Terminating with finalizer `{self.finalizer}`. "
                f"The finalizer is owned by Deployment `{self.controller_name}` using ServiceAccount "
                f"`{self.sa_name}`, but ClusterRole `{self.clusterrole_name}` was changed to read-only and lacks "
                "permission to patch ConfigMaps, so the controller logs HTTP 403 Forbidden while trying to remove "
                "the finalizer. Restoring the controller's RBAC lets its reconcile loop remove finalizers so both "
                "the current ConfigMap and future cleanup requests are deleted. In other words, the ServiceAccount "
                f"`{self.sa_name}` lacks RBAC permission to patch ConfigMaps; naming that missing ServiceAccount "
                "permission is a correct localization even without naming the ClusterRole."
            ),
            oracle_factory=lambda problem: FinalizerDeadlockControllerMitigationOracle(
                problem=problem,
                configmap_name=problem.configmap_name,
                finalizer=problem.finalizer,
                controller_deployment_name=problem.controller_name,
            ),
        )
        self.rbac_v1 = client.RbacAuthorizationV1Api()


class ServiceWrongPodSelectionIA(Problem):
    """Frappe's web Service also selects the long-queue worker pods (no HTTP port)."""

    ROUTE_LABEL_KEY = "service-route"

    def __init__(
        self,
        app_name: str = "frappe",
        service: str = "svc-frappe-web",
        target_deployment: str = "erp-gunicorn",
        wrong_deployment: str = "erp-worker-l",
        port: int = 8000,
    ):
        super().__init__(app=make_app(app_name))
        self.frontend_service = service
        self.target_deployment = target_deployment
        self.wrong_deployment = wrong_deployment
        self.expected_service_port = port
        self.route_label_key = self.ROUTE_LABEL_KEY
        self.route_label_value = service
        self.faulty_service_selector = {self.route_label_key: self.route_label_value}
        self.expected_endpoint_pod_label = target_deployment
        self.original_selector: dict | None = None
        self.original_target_port: str | int | None = None
        configure_port(
            self,
            app_name,
            component=f"service/{service}",
            description=(
                f"The `{service}` Service selector was broadened to `{self.route_label_key}={service}`, and that "
                f"label is present on both the intended `{target_deployment}` pods and the unrelated "
                f"`{wrong_deployment}` pods. The Service still has endpoints, but the endpoint list includes "
                f"`{wrong_deployment}` pods, which do not listen on port {port}, so part of the traffic sent "
                "through the Service is refused."
            ),
            oracle_factory=WrongPodSelectionMitigationOracle,
        )

    def _patch_route_label(self, deployment: str, value) -> None:
        patch = {"spec": {"template": {"metadata": {"labels": {self.route_label_key: value}}}}}
        self.kubectl.patch_deployment(deployment, self.namespace, patch)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{deployment} -n {self.namespace} --timeout=600s", timeout=630
        )

    def _set_route(self, selector: dict, target_port: str | int) -> None:
        service = self.kubectl.core_v1_api.read_namespaced_service(self.frontend_service, self.namespace)
        index = next(i for i, port in enumerate(service.spec.ports) if port.port == self.expected_service_port)
        patch = json.dumps(
            [
                {"op": "replace", "path": "/spec/selector", "value": selector},
                {"op": "replace", "path": f"/spec/ports/{index}/targetPort", "value": target_port},
            ]
        )
        self.kubectl.exec_command_checked(
            f"kubectl patch svc {self.frontend_service} -n {self.namespace} --type=json -p='{patch}'"
        )

    @mark_fault_injected
    def inject_fault(self):
        service = self.kubectl.core_v1_api.read_namespaced_service(self.frontend_service, self.namespace)
        if self.original_selector is None:
            self.original_selector = dict(service.spec.selector or {})
            port = next(port for port in service.spec.ports if port.port == self.expected_service_port)
            self.original_target_port = port.target_port or port.port
        for deployment in (self.target_deployment, self.wrong_deployment):
            self._patch_route_label(deployment, self.route_label_value)
        # A named port excludes workers that do not declare it. A numeric port
        # makes kube-proxy route to their IPs, where no HTTP server listens.
        self._set_route(self.faulty_service_selector, self.expected_service_port)
        print(f"Service {self.frontend_service} now selects {self.faulty_service_selector}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if self.original_selector is None:
            print("No Service route was changed by this problem; nothing to restore")
            return
        selector = self.original_selector
        if not selector:
            deployment = self.kubectl.get_deployment(self.target_deployment, self.namespace)
            selector = dict(deployment.spec.selector.match_labels)
        self._set_route(selector, self.original_target_port or self.expected_service_port)
        for deployment in (self.target_deployment, self.wrong_deployment):
            self._patch_route_label(deployment, None)


class RollingUpdateMisconfiguredIA(Problem):
    """An all-at-once rollout strategy meets an init container that never finishes.

    The original problem planted its own Deployment; the port applies the same
    change to a real worker Deployment so the outage reaches the app's users
    (background jobs stop being processed).
    """

    HANG_INIT = {
        "name": "hang-init",
        "image": "busybox:1.36",
        "command": ["sh", "-c", "sleep infinity"],
    }

    def __init__(self, app_name: str = "frappe", faulty_service: str = "erp-worker-l"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.saved: dict | None = None
        configure_port(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` uses a RollingUpdate strategy with maxUnavailable=100% and "
                "maxSurge=0, and its pod template gained an init container (`hang-init`) that never completes. "
                "The last rollout therefore terminated every old replica before any new one became Ready, so the "
                "deployment is stuck with zero available replicas and the work it performs has stopped."
            ),
            oracle_factory=lambda problem: RollingUpdateMitigationOracle(
                problem=problem, deployment_name=problem.faulty_service
            ),
        )

    @property
    def _state_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-{self.faulty_service}-rollout.json")

    @mark_fault_injected
    def inject_fault(self):
        deployment = self.kubectl.exec_command_checked(
            f"kubectl get deployment {self.faulty_service} -n {self.namespace} -o json"
        )
        spec = json.loads(deployment)["spec"]
        self.saved = {
            "strategy": spec.get("strategy"),
            "initContainers": spec["template"]["spec"].get("initContainers"),
        }
        self._state_path.write_text(json.dumps(self.saved))
        patch = {
            "spec": {
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "100%", "maxSurge": "0%"}},
                "template": {
                    "spec": {"initContainers": [*(self.saved["initContainers"] or []), self.HANG_INIT]},
                },
            }
        }
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=merge -p '{json.dumps(patch)}'"
        )
        # Wait until the old replicas are gone, which is the outage.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = self.kubectl.get_deployment(self.faulty_service, self.namespace)
            if not (current.status.available_replicas or 0):
                break
            time.sleep(5)
        print(f"Deployment {self.faulty_service} rolled out with a hanging init container and no availability")

    @mark_fault_injected
    def recover_fault(self):
        saved = self.saved or json.loads(self._state_path.read_text())
        patch = {
            "spec": {
                "strategy": saved["strategy"] or {"type": "RollingUpdate", "rollingUpdate": None},
                "template": {"spec": {"initContainers": saved["initContainers"]}},
            }
        }
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=merge -p '{json.dumps(patch)}'"
        )
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=600s",
            timeout=630,
        )
        self._state_path.unlink(missing_ok=True)
