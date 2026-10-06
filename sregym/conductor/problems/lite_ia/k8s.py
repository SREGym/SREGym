"""SREGym-Lite Kubernetes-level faults re-targeted at the Incident Arena apps.

Each class reuses the original problem's injection and recovery and replaces
only its constructor: the app, the target component, the ground-truth text
and the mitigation oracle (wrapped with :func:`with_app_health`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from kubernetes import client

from sregym.conductor.oracles.admission_webhook_outage_mitigation import AdmissionWebhookOutageMitigationOracle
from sregym.conductor.oracles.cronjob_sidecar_mitigation import CronJobSidecarBlocksCompletionMitigationOracle
from sregym.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle
from sregym.conductor.oracles.duplicate_pvc_mounts_mitigation import DuplicatePVCMountsMitigationOracle
from sregym.conductor.oracles.finalizer_deadlock_controller_mitigation import (
    FinalizerDeadlockControllerMitigationOracle,
)
from sregym.conductor.oracles.internal_traffic_policy_mitigation import InternalTrafficPolicyMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mutating_webhook_resource_limits_mitigation import (
    MutatingWebhookResourceLimitsMitigationOracle,
)
from sregym.conductor.oracles.network_policy_oracle import NetworkPolicyMitigationOracle
from sregym.conductor.oracles.readiness_probe_mitigation import ReadinessProbeMitigationOracle
from sregym.conductor.oracles.rolling_update_misconfiguration_mitigation import RollingUpdateMitigationOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.conductor.oracles.wrong_pod_selection_mitigation import WrongPodSelectionMitigationOracle
from sregym.conductor.problems import finalizer_deadlock_controller as finalizer_module
from sregym.conductor.problems.admission_webhook_outage import AdmissionWebhookOutage
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.cronjob_sidecar_blocks_completion import (
    CronJobSidecarBlocksCompletionHotelReservation,
)
from sregym.conductor.problems.duplicate_pvc_mounts import DuplicatePVCMounts
from sregym.conductor.problems.finalizer_deadlock_controller import FinalizerDeadlockController
from sregym.conductor.problems.internal_traffic_policy_local import InternalTrafficPolicyLocalAstronomyShop
from sregym.conductor.problems.lite_ia.common import BASELINE_S, PROPAGATION_S, make_app, with_app_health
from sregym.conductor.problems.mutating_webhook_resource_limits import MutatingWebhookResourceLimits
from sregym.conductor.problems.network_policy_block import NetworkPolicyBlock
from sregym.conductor.problems.readiness_probe_misconfiguration import ReadinessProbeMisconfiguration
from sregym.conductor.problems.service_dns_resolution_failure import ServiceDNSResolutionFailure
from sregym.conductor.problems.service_wrong_pod_selection_hotel_reservation import (
    ServiceWrongPodSelectionHotelReservation,
)
from sregym.conductor.problems.wrong_dns_policy import WrongDNSPolicy
from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


def ported(problem: Problem, app_name: str, *, component: str, description: str, oracle_factory) -> None:
    """Shared constructor body: app, timing, ground truth and oracles."""
    Problem.__init__(problem, app=make_app(app_name))
    problem.app_name = app_name
    problem.kubectl = KubeCtl()
    problem.baseline_duration_s = BASELINE_S
    problem.propagation_duration_s = PROPAGATION_S
    problem.root_cause = problem.build_structured_root_cause(
        component=component, namespace=problem.namespace, description=description
    )
    problem.diagnosis_oracle = LLMAsAJudgeOracle(problem=problem, expected=problem.root_cause)
    problem.mitigation_oracle = with_app_health(problem, oracle_factory(problem))
    problem.app.create_workload()


def reopen_connections(problem: Problem, deployment: str, timeout_s: int = 300) -> None:
    """Roll ``deployment`` so the connections through the faulted hop are re-opened under the fault.

    Slack Spine's clients (undici ``fetch`` in the svc-* roles, aiohttp in the
    load generator) keep HTTP connections alive, and established connections
    survive Service selector, CoreDNS and NetworkPolicy changes. Without a
    restart those faults reach users only as connections happen to recycle; a
    routine rollout of a deployment on the path makes them bite immediately.
    """
    problem.kubectl.exec_command_checked(f"kubectl rollout restart deployment/{deployment} -n {problem.namespace}")
    problem.kubectl.exec_command_checked(
        f"kubectl rollout status deployment/{deployment} -n {problem.namespace} --timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )


class ReadinessProbeMisconfigurationIA(ReadinessProbeMisconfiguration):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"The deployment `{faulty_service}` has a misconfigured readiness probe that targets a non-existent "
                "health endpoint (`/healthz` on port `8080`; the service listens on 8000), so its pods fail "
                "readiness checks and remain NotReady. Kubernetes excludes them from the Service endpoints even "
                "though the containers keep running, so every request path through this service fails."
            ),
            oracle_factory=ReadinessProbeMitigationOracle,
        )


class ServiceDNSResolutionFailureIA(ServiceDNSResolutionFailure):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-thread"):
        self.faulty_service = faulty_service
        ported(
            self,
            app_name,
            component=f"configmap/coredns in kube-system (NXDOMAIN template for `{faulty_service}`)",
            description=(
                f"CoreDNS (kube-system/coredns Corefile) is configured with an NXDOMAIN template for "
                f"`{faulty_service}.<namespace>.svc.cluster.local`, so in-cluster lookups for this service name "
                "fail at DNS resolution time. Clients that call it by name cannot resolve it even though its pods "
                "are healthy and listening, so thread requests fail with name-resolution errors."
            ),
            oracle_factory=DNSResolutionMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # Clients (the load generator's thread/reply sessions) must re-resolve the name.
        reopen_connections(self, self.faulty_service)


class WrongServiceSelectorIA(WrongServiceSelector):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-channel", port: int = 8000):
        self.faulty_service = faulty_service
        self.expected_service_port = port
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"The Service `{faulty_service}` has a misconfigured selector with an extra label "
                f"(`current_service_name: {faulty_service}`) that no pod carries, so it no longer matches its "
                "backing pods. The Service has zero endpoints despite a healthy Deployment, and calls routed "
                "through it fail with connection errors (svc-message's channel authorization check, so message "
                "sends fail)."
            ),
            oracle_factory=ServiceEndpointMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # Kept-alive connections to the old pods would otherwise keep working.
        reopen_connections(self, self.faulty_service)


class WrongDNSPolicyIA(WrongDNSPolicy):
    """Saleor's API pods get an external-only resolver.

    Saleor's readiness endpoint does not touch its backends, so the
    misconfigured pod becomes Ready and serves, as Astronomy Shop's frontend
    did. (Frappe and most Slack roles probe their backends by name, so a
    resolver change there only stalls the rollout behind the healthy pod.)
    """

    def __init__(
        self, app_name: str = "saleor", faulty_service: str = "saleor-api", service_name: str = "svc-saleor-api"
    ):
        self.faulty_service = faulty_service
        self.service_name = service_name
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` is configured with `dnsPolicy: None` and an external resolver "
                "(`8.8.8.8`), so its pods cannot resolve cluster-internal service names such as `postgres`, "
                "`valkey` and `rabbitmq`. Its health endpoint does not touch those dependencies, so the pod is "
                "Ready and receives traffic, but every request that needs the database fails with name-resolution "
                "errors even though the dependencies are healthy."
            ),
            oracle_factory=DNSResolutionMitigationOracle,
        )


class DuplicatePVCMountsIA(DuplicatePVCMounts):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-search"):
        self.faulty_service = faulty_service
        # The target's own readiness endpoint replaces Jaeger's query API.
        self.health_check = (8000, "/healthz", "")
        ported(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The deployment `{faulty_service}` is scaled to multiple replicas that all mount one "
                f"ReadWriteOnce PVC (`{faulty_service}-pvc`) while required hostname podAntiAffinity places those "
                "replicas on different nodes. With node-bound storage only one replica can use the claim, so "
                "another replica stays Pending and the deployment remains only partially Ready."
            ),
            oracle_factory=DuplicatePVCMountsMitigationOracle,
        )


class AdmissionWebhookOutageIA(AdmissionWebhookOutage):
    def __init__(
        self, app_name: str = "saleor", faulty_service: str = "saleor-api", service_name: str = "svc-saleor-api"
    ):
        self.faulty_service = faulty_service
        self.service_name = service_name
        # saleor-api runs one replica behind the load generator: the oracle's
        # admission probe must not delete it (that outage alone would fail
        # LoadgenHealthOracle), so it proves recreation with a surge rollout.
        self.recreation_probe = "rollout"
        ported(
            self,
            app_name,
            component=f"ValidatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            description=(
                f"A ValidatingWebhookConfiguration `{self.WEBHOOK_NAME}` with `failurePolicy: Fail` intercepts "
                f"pod CREATE in the namespace and calls `{self.BACKEND_SVC_NAME}` in namespace "
                f"`{self.BACKEND_SVC_NAMESPACE}`, which does not exist, so every pod creation is rejected with a "
                f"`failed calling webhook` error. The ReplicaSet of the `{faulty_service}` deployment cannot "
                f"recreate a deleted pod, leaving the deployment with no Ready replica and Service "
                f"`{service_name}` without endpoints, although the deployment's own spec is healthy."
            ),
            oracle_factory=AdmissionWebhookOutageMitigationOracle,
        )
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()


class CronJobSidecarBlocksCompletionIA(CronJobSidecarBlocksCompletionHotelReservation):
    def __init__(self, app_name: str = "frappe"):
        self.faulty_service = self.CRONJOB_NAME
        self.cronjob_name = self.CRONJOB_NAME
        ported(
            self,
            app_name,
            component=f"CronJob/{self.CRONJOB_NAME}",
            description=(
                f"The CronJob '{self.CRONJOB_NAME}' schedules a pod every minute whose jobTemplate has two regular "
                f"containers: a primary ('{self.PRIMARY_CONTAINER}') that performs a short archival step and exits "
                f"cleanly, and a log-shipper sidecar ('{self.SIDECAR_CONTAINER}') that never terminates. A Job is "
                "Complete only when every container in its pod terminates, so each pod stays Running after the "
                "primary exits and each schedule adds another active Job that never completes; "
                "successfulJobsHistoryLimit does not apply to active Jobs, so they accumulate. The fix is to make "
                "the sidecar a native sidecar (an initContainer with restartPolicy: Always) so it is stopped when "
                "the primary exits, and to clean up the accumulated active Jobs; activeDeadlineSeconds, removing "
                "the sidecar, or deleting the CronJob are not acceptable."
            ),
            oracle_factory=CronJobSidecarBlocksCompletionMitigationOracle,
        )
        self.batch_v1 = client.BatchV1Api()
        self.core_v1 = client.CoreV1Api()


class FinalizerDeadlockControllerIA(FinalizerDeadlockController):
    def __init__(self, app_name: str = "frappe"):
        self.configmap_name = finalizer_module._CONFIGMAP_NAME
        self.finalizer = finalizer_module._FINALIZER
        self.controller_name = finalizer_module._CONTROLLER_NAME
        self.sa_name = finalizer_module._SA_NAME
        self.clusterrole_name = finalizer_module._CLUSTERROLE_NAME
        self.clusterrolebinding_name = finalizer_module._CLUSTERROLEBINDING_NAME
        self.faulty_service = self.configmap_name
        ported(
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


class NetworkPolicyBlockIA(NetworkPolicyBlock):
    def __init__(
        self, app_name: str = "slack_spine", faulty_service: str = "svc-auth", client_deployment: str = "svc-message"
    ):
        self.faulty_service = faulty_service
        self.client_deployment = client_deployment
        self.policy_name = f"deny-all-{faulty_service}"
        # Probe from the frontend's network identity: it must reach the target
        # and the frontend must still answer its readiness endpoint.
        self.probe_path = "/healthz"
        self.probe_expect = ""
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"A NetworkPolicy `{self.policy_name}` blocks all ingress and egress traffic for the pods of "
                f"deployment `{faulty_service}`, isolating it from the network. Calls to and from it fail, so "
                f"every request path that depends on it breaks (logins, and `{client_deployment}`'s session check "
                "on every message send) even though its pods remain Running and Ready."
            ),
            oracle_factory=NetworkPolicyMitigationOracle,
        )
        self.networking_v1 = client.NetworkingV1Api()

    @mark_fault_injected
    def inject_fault(self):
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": self.policy_name, "namespace": self.namespace},
            "spec": {
                "podSelector": {"matchLabels": dict(deployment.spec.selector.match_labels)},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }
        self.networking_v1.create_namespaced_network_policy(namespace=self.namespace, body=policy)
        # Established connections survive the policy (and a restarted svc-auth pod could not even reach
        # its database), so roll the client that checks sessions on every send.
        reopen_connections(self, self.client_deployment)


class ServiceWrongPodSelectionIA(ServiceWrongPodSelectionHotelReservation):
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
        self.frontend_service = service
        self.target_deployment = target_deployment
        self.wrong_deployment = wrong_deployment
        self.expected_service_port = port
        self.route_label_key = self.ROUTE_LABEL_KEY
        self.route_label_value = service
        self.faulty_service_selector = {self.route_label_key: self.route_label_value}
        self.expected_endpoint_pod_label = target_deployment
        self.original_selector: dict | None = None
        ported(
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

    def _set_selector(self, selector: dict) -> None:
        patch = json.dumps([{"op": "replace", "path": "/spec/selector", "value": selector}])
        self.kubectl.exec_command_checked(
            f"kubectl patch svc {self.frontend_service} -n {self.namespace} --type=json -p='{patch}'"
        )

    @mark_fault_injected
    def inject_fault(self):
        service = self.kubectl.core_v1_api.read_namespaced_service(self.frontend_service, self.namespace)
        self.original_selector = dict(service.spec.selector or {})
        for deployment in (self.target_deployment, self.wrong_deployment):
            self._patch_route_label(deployment, self.route_label_value)
        self._set_selector(self.faulty_service_selector)
        print(f"Service {self.frontend_service} now selects {self.faulty_service_selector}")

    @mark_fault_injected
    def recover_fault(self):
        selector = self.original_selector
        if not selector:
            deployment = self.kubectl.get_deployment(self.target_deployment, self.namespace)
            selector = dict(deployment.spec.selector.match_labels)
        self._set_selector(selector)
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
        self.faulty_service = faulty_service
        self.saved: dict | None = None
        ported(
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


class InternalTrafficPolicyLocalIA(InternalTrafficPolicyLocalAstronomyShop):
    """``internalTrafficPolicy: Local`` on Slack Spine's notification Service, its caller on another node."""

    FAULTY_SERVICE = "svc-notification"
    SERVICE_PORT = 8000
    POD_LABEL_SELECTOR = "app.kubernetes.io/component=svc-notification"
    CALLER_SERVICE = "svc-message"
    CALLER_POD_LABEL_SELECTOR = "app.kubernetes.io/component=svc-message"

    def __init__(self, app_name: str = "slack_spine"):
        self.faulty_service = self.FAULTY_SERVICE
        self.pod_node: str | None = None
        self.victim_node: str | None = None
        ported(
            self,
            app_name,
            component=f"service/{self.FAULTY_SERVICE}",
            description=(
                f"The `{self.FAULTY_SERVICE}` Service has `spec.internalTrafficPolicy: Local`, so kube-proxy "
                "routes in-cluster traffic only to a pod on the caller's own node. The deployment has a single "
                "replica pinned to one worker, while its clients run on other nodes (the users' unread-count "
                "requests, `GET /unread`, and svc-message's notification fan-out), so their connections are "
                "silently dropped and hang until the client times out. The pod is "
                "Running and Ready and the endpoints are populated; the fault is only visible in "
                "`internalTrafficPolicy` and the pod placement. Valid mitigations: set the policy back to "
                "`Cluster` (or remove it), or run a ready pod on every worker node."
            ),
            oracle_factory=InternalTrafficPolicyMitigationOracle,
        )
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

    LOADGEN_POD_LABEL_SELECTOR = "app.kubernetes.io/component=loadgen"

    def _select_nodes(self) -> tuple[str, str]:
        """Pin the backend away from the load generator's node.

        The load generator calls ``svc-notification`` directly (``GET /unread``),
        while svc-message's notify fan-out is fire-and-forget. If the backend
        landed on the load generator's node, ``Local`` would still route its
        traffic and users would see nothing, so the victim node is the load
        generator's own (it is not restarted, keeping its ledger).
        """
        workers = self.worker_nodes()
        if len(workers) < 2:
            raise RuntimeError("internal_traffic_policy_local requires at least two worker nodes")
        loadgen_nodes = sorted(self._nodes_with_running_pod(self.LOADGEN_POD_LABEL_SELECTOR) & set(workers))
        victim_node = loadgen_nodes[0] if loadgen_nodes else workers[1]
        pod_node = next(node for node in workers if node != victim_node)
        return pod_node, victim_node

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # Connections opened before the policy change survive it (conntrack),
        # and the load generator keeps its keep-alive connections open. Replace
        # the backend pod (still pinned to pod_node) so they all have to reconnect.
        self.kubectl.exec_command(f"kubectl rollout restart deployment/{self.FAULTY_SERVICE} -n {self.namespace}")
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.FAULTY_SERVICE} -n {self.namespace} --timeout=300s"
        )
        self._wait_for_pod_on_node(self.pod_node)
        print(f"Replaced the {self.FAULTY_SERVICE} pod on {self.pod_node} to drop pre-existing connections")


class MutatingWebhookResourceLimitsIA(MutatingWebhookResourceLimits):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        self.ca_bundle = None
        ported(
            self,
            app_name,
            component=f"MutatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            description=(
                f"The fault is the cluster-scoped MutatingWebhookConfiguration `{self.WEBHOOK_NAME}`. It "
                "intercepts every pod CREATE in the application namespace and patches the first container's "
                f"`resources.requests.memory` and `resources.limits.memory` to `{self.INJECTED_MEMORY}`. Four "
                "companion MutatingWebhookConfigurations share its namespaceSelector and backend Service but stay "
                "inert (rules for uninstalled CRDs, or objectSelectors requiring labels no pod carries); the "
                f"webhook backend works as configured. Deployment `{faulty_service}` declares legitimate memory "
                f"(`requests: {self.SPEC_MEMORY_REQUEST}`, `limits: {self.SPEC_MEMORY_LIMIT}`) but its recreated "
                f"pod runs with `{self.INJECTED_MEMORY}` and is OOMKilled on startup; the gap between the "
                "Deployment spec and the running pod's resources is the diagnostic signal."
            ),
            oracle_factory=MutatingWebhookResourceLimitsMitigationOracle,
        )
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()
