"""SREGym problems ported to Saleor (Django/GraphQL API + Celery worker, Postgres, Valkey, RabbitMQ).

Each class keeps the original problem's injection mechanism and mitigation
oracle, re-targeted at a real Saleor component, and replaces only what the new
app requires (constructor via :func:`ported`, label lookups, images, requests).
"""

from __future__ import annotations

import copy
import time

from kubernetes import client

from sregym.conductor.oracles.assign_non_existent_node_mitigation import AssignNonExistentNodeMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.nightly_rebalance_oom_mitigation import NightlyRebalanceOOMMitigationOracle
from sregym.conductor.oracles.priority_preemption_mitigation import PriorityPreemptionMitigationOracle
from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.oracles.wrong_bin_mitigation import WrongBinMitigationOracle
from sregym.conductor.problems.assign_non_existent_node import AssignNonExistentNode
from sregym.conductor.problems.kubelet_crash import KubeletCrash
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.conductor.problems.liveness_probe_too_aggressive import LivenessProbeTooAggressive
from sregym.conductor.problems.nightly_rebalance_oom import NightlyRebalanceOOM
from sregym.conductor.problems.priority_preemption_cascade import PriorityPreemptionCascadeHotelReservation
from sregym.conductor.problems.pvc_claim_mismatch import PVCClaimMismatch
from sregym.conductor.problems.rbac_misconfiguration import RBACMisconfiguration
from sregym.conductor.problems.resource_request import ResourceRequestTooSmall
from sregym.conductor.problems.sidecar_port_conflict import SidecarPortConflict
from sregym.conductor.problems.wrong_bin_usage import WrongBinUsage
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected

APP = "saleor"
API = "saleor-api"
WORKER = "saleor-worker"
API_SERVICE = "svc-saleor-api"


def _replace_deployment(injector: VirtualizationFaultInjector, service: str, deployment_yaml: dict) -> None:
    """Delete and re-apply ``service`` from ``deployment_yaml`` (the original injectors' replace step)."""
    path = injector._write_yaml_to_file(f"{service}-faulty", deployment_yaml)
    injector.kubectl.exec_command(f"kubectl delete deployment {service} -n {injector.namespace}")
    injector.kubectl.exec_command(f"kubectl apply -f {path} -n {injector.namespace}")


# ----------------------------------------------------------------------------- assign_to_non_existent_node
class AssignNonExistentNodeSaleor(AssignNonExistentNode):
    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` (Saleor's GraphQL API, the only backend of Service "
                f"`{API_SERVICE}`) has a `nodeSelector` `kubernetes.io/hostname: extra-node` that matches no node in "
                "the cluster, so the scheduler cannot place its pod. The pod stays Pending with a node-affinity/"
                "selector mismatch (FailedScheduling) and never becomes Ready, the Service has no endpoints, and "
                "every storefront/checkout GraphQL request fails."
            ),
            oracle_factory=AssignNonExistentNodeMitigationOracle,
        )


# ----------------------------------------------------------------------------- wrong_bin_usage
class WrongBinUsageSaleor(WrongBinUsage):
    """saleor-api runs the image's Celery worker entrypoint instead of uvicorn."""

    def __init__(self, faulty_service: str = API, wrong_binary_source: str = WORKER):
        self.faulty_service = faulty_service
        self.wrong_binary_source = wrong_binary_source
        # WrongBinMitigationOracle: the API container's command must run this binary again.
        self.expected_command = "uvicorn"
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The container command of deployment `{faulty_service}` was replaced with the Celery worker "
                f"entrypoint of `{wrong_binary_source}` (`celery -A saleor --app=saleor.celeryconf:app worker ...`) "
                "instead of the ASGI server (`uvicorn saleor.asgi:application --port=8000 ...`). Both are valid "
                "binaries in the same image, so the pod starts, but nothing listens on port 8000: readiness "
                "(`/health/`) and liveness (tcp 8000) probes fail, the pod restarts and never becomes Ready, and "
                f"Service `{API_SERVICE}` has no endpoints, so all GraphQL API requests fail."
            ),
            oracle_factory=WrongBinMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        source = injector._get_deployment_yaml(self.wrong_binary_source)
        wrong_command = source["spec"]["template"]["spec"]["containers"][0]["command"]
        deployment = injector._get_deployment_yaml(self.faulty_service)
        injector._write_yaml_to_file(self.faulty_service, copy.deepcopy(deployment))
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        print(f"Changing {self.faulty_service}/{container['name']} command to {wrong_command}")
        container["command"] = list(wrong_command)
        _replace_deployment(injector, self.faulty_service, deployment)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.kubectl.exec_command(f"kubectl delete deployment {self.faulty_service} -n {self.namespace}")
        self.kubectl.exec_command(f"kubectl apply -f /tmp/{self.faulty_service}_modified.yaml -n {self.namespace}")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")


# ----------------------------------------------------------------------------- sidecar_port_conflict
class SidecarPortConflictSaleor(SidecarPortConflict):
    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` gained a sidecar container (`sidecar`, busybox `nc -lk -p 8000`) "
                "that binds the same port 8000 as the uvicorn API container in the same pod network namespace. "
                "Whichever process binds second fails (`address already in use`), so one container crash-loops and "
                f"the pod never becomes Ready; Service `{API_SERVICE}` has no endpoints and the GraphQL API is down."
            ),
            oracle_factory=MitigationOracle,
        )


# ----------------------------------------------------------------------------- liveness_probe_too_aggressive
class LivenessProbeTooAggressiveSaleor(LivenessProbeTooAggressive):
    """The original's aggressive probe on saleor-api (uvicorn needs seconds to import Django and bind).

    The original first swapped in a deliberately slow custom service; Saleor's
    API is slow to start on its own, so the probe change is applied to it as is.
    """

    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` uses an overly aggressive liveness probe (tcpSocket 8000 with "
                "`initialDelaySeconds=0`, `periodSeconds=1`, `failureThreshold=1`) and "
                "`terminationGracePeriodSeconds=0`. uvicorn needs several seconds to import Saleor/Django and bind "
                "port 8000, so the first probe fails and the kubelet kills the container before it can serve; the "
                f"pod restarts in a loop (CrashLoopBackOff) and never stays Ready, leaving Service `{API_SERVICE}` "
                "without endpoints and API requests failing."
            ),
            oracle_factory=lambda problem: SustainedReadinessOracle(problem=problem, sustained_period=30),
        )
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = self.injector
        deployment = injector._get_deployment_yaml(self.faulty_service)
        injector._write_yaml_to_file(self.faulty_service, copy.deepcopy(deployment))
        pod_spec = deployment["spec"]["template"]["spec"]
        for container in pod_spec["containers"]:
            probe = container.get("livenessProbe")
            if probe:
                probe["initialDelaySeconds"] = 0
                probe["periodSeconds"] = 1
                probe["failureThreshold"] = 1
        pod_spec["terminationGracePeriodSeconds"] = 0
        _replace_deployment(injector, self.faulty_service, deployment)
        injector.kubectl.wait_for_stable(self.namespace)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")


# ----------------------------------------------------------------------------- pvc_claim_mismatch
class PVCClaimMismatchSaleor(PVCClaimMismatch):
    def __init__(self):
        self.faulty_service = [API, WORKER]
        ported(
            self,
            APP,
            component="saleor-media",
            description=(
                f"Deployments `{API}` and `{WORKER}` reference a non-existent PersistentVolumeClaim "
                "(`saleor-media-broken` instead of `saleor-media`) for their shared `/app/media` volume. The "
                "scheduler cannot bind the claim, so both pods stay Pending (`persistentvolumeclaim "
                '"saleor-media-broken" not found`) and never start: the GraphQL API has no endpoints and Celery '
                "tasks are not processed, although the real `saleor-media` claim is Bound and healthy."
            ),
            oracle_factory=MitigationOracle,
        )
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)


# ----------------------------------------------------------------------------- rbac_misconfiguration
class RBACMisconfigurationSaleor(RBACMisconfiguration):
    """The init container's kubectl needs a mounted token to reach the RBAC check, so automount is forced on."""

    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` now runs as ServiceAccount `{faulty_service}-rbac-sa` and has an "
                "init container `config-loader` that runs `kubectl get configmap app-routing-config`. The "
                f"ClusterRole `{faulty_service}-rbac-role` bound to that ServiceAccount only grants get/list/watch "
                "on pods and services, not configmaps, so the init container fails with a Forbidden error and the "
                f"pod stays in Init:Error/Init:CrashLoopBackOff; Service `{API_SERVICE}` has no endpoints and the "
                "GraphQL API is down. The fix is to grant configmap read access to the ServiceAccount (or remove the "
                "dependency), not to delete the deployment."
            ),
            oracle_factory=MitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: RBAC Init Container Misconfiguration ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        original = injector._get_deployment_yaml(self.faulty_service)
        # Incident Arena charts may disable automountServiceAccountToken; the new ServiceAccount's token
        # must be mounted for the init container to reach the API server and be denied by RBAC.
        self.kubectl.patch_deployment(
            self.faulty_service,
            self.namespace,
            {"spec": {"template": {"spec": {"automountServiceAccountToken": True}}}},
        )
        injector._inject(fault_type="rbac_misconfiguration", microservices=[self.faulty_service])
        # Recovery re-applies the true original (token automount off), not the patched copy.
        injector._write_yaml_to_file(f"{self.faulty_service}-original", original)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")


# ----------------------------------------------------------------------------- resource_request_too_small
class ResourceRequestTooSmallSaleor(ResourceRequestTooSmall):
    """On saleor-api: the load generator never waits on Celery, so a worker outage is invisible to users."""

    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` has an extremely low memory limit (`10Mi`, with the request lowered "
                "to match), far below what the Python/Django process needs to start, so the container is OOMKilled "
                "on every start and the pod crash-loops (CrashLoopBackOff, last state OOMKilled) without becoming "
                f"Ready; Service `{API_SERVICE}` loses its endpoint and GraphQL API requests fail."
            ),
            oracle_factory=MitigationOracle,
        )

    def set_memory_limit(self, deployment_yaml):
        dyaml = copy.deepcopy(deployment_yaml)
        resources = dyaml["spec"]["template"]["spec"]["containers"][0].setdefault("resources", {})
        resources.setdefault("limits", {})["memory"] = "10Mi"
        # The chart's memory request is larger than 10Mi, which the API server would reject.
        resources.setdefault("requests", {})["memory"] = "10Mi"
        print(f"Setting memory limit and request to 10Mi for {self.faulty_service}")
        return dyaml


# ----------------------------------------------------------------------------- nightly_rebalance_oom
class NightlyRebalanceOOMSaleor(NightlyRebalanceOOM):
    """On saleor-api: the load generator never waits on Celery, so a worker outage is invisible to users."""

    app_namespace = "saleor"
    # bitnami/kubectl is no longer pulled freely from Docker Hub.
    actor_image = "alpine/k8s:1.28.3"

    def __init__(self, faulty_service: str = API):
        self.faulty_service = faulty_service
        self._target_container = None
        self._original_memory_limit = None
        self._original_memory_request = None
        ported(
            self,
            APP,
            component=f"{self.actor_name} CronJob ({self.actor_namespace})",
            description=(
                f"The fault originates from the scheduled `{self.actor_name}` CronJob in `{self.actor_namespace}` "
                f"(policy ConfigMap `{self.policy_configmap}`), which every minute patches deployment "
                f"`{faulty_service}` (Saleor's GraphQL API) down to a `{self.squeeze_memory}` memory limit and "
                "request. That is below the process's startup working set, so the container is OOMKilled during "
                f"start and stays in CrashLoopBackOff; Service `{API_SERVICE}` has no endpoints and API requests "
                "fail. A diagnosis that only "
                f"names the OOMKilled `{faulty_service}` pod or its memory limit without identifying the recurring "
                f"`{self.actor_name}` actor is incomplete. A durable fix must suspend/remove the `{self.actor_name}` "
                f"CronJob or correct its policy, and restore a sane memory limit on `{faulty_service}`."
            ),
            oracle_factory=NightlyRebalanceOOMMitigationOracle,
        )

    @property
    def target_pod_labels(self) -> dict:
        """Labels of the target's pods (read by NightlyRebalanceOOMMitigationOracle)."""
        return dict(self.kubectl.get_deployment(self.faulty_service, self.namespace).spec.selector.match_labels)

    @mark_fault_injected
    def inject_fault(self):
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        containers = dep.spec.template.spec.containers
        container = next((c for c in containers if c.name == self.faulty_service), containers[0])
        requests = (container.resources.requests or {}) if container.resources else {}
        self._original_memory_request = requests.get("memory")
        super().inject_fault()
        # The squeezed pod must actually replace the healthy one: with a surge rollout the old pod
        # would keep serving behind the crash-looping new one and users would never see the OOM loop.
        self._retire_old_replica_sets()

    def _retire_old_replica_sets(self):
        """Delete the target's pre-squeeze ReplicaSets (and their pods).

        Deleting only the old pod is not enough: the old ReplicaSet recreates it
        while the rollout waits for the crash-looping new pod. Removing the old
        ReplicaSets leaves the Deployment spec untouched (the actor's patch is
        the only change) and the squeezed ReplicaSet as the only one.
        """
        apps = client.AppsV1Api()
        deployment = apps.read_namespaced_deployment(self.faulty_service, self.namespace)
        selector = ",".join(f"{k}={v}" for k, v in deployment.spec.selector.match_labels.items())
        for rs in apps.list_namespaced_replica_set(self.namespace, label_selector=selector).items:
            if not any(ref.uid == deployment.metadata.uid for ref in rs.metadata.owner_references or []):
                continue
            limits = {}
            for c in rs.spec.template.spec.containers:
                if c.name == self._target_container and c.resources and c.resources.limits:
                    limits = c.resources.limits
            if limits.get("memory") != self.squeeze_memory:
                print(f"Deleting pre-squeeze ReplicaSet {rs.metadata.name}")
                apps.delete_namespaced_replica_set(
                    rs.metadata.name, self.namespace, propagation_policy="Background"
                )

    def _squeeze_patch(self) -> dict:
        memory = {"memory": self.squeeze_memory}
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            # The chart's request exceeds the squeezed limit, which the API server rejects.
                            {"name": self._target_container, "resources": {"limits": memory, "requests": memory}}
                        ]
                    }
                }
            }
        }

    def _wait_for_target_unhealthy(self, timeout: int):
        unhealthy_reasons = {"CrashLoopBackOff", "CreateContainerError", "RunContainerError", "StartError"}
        selector = ",".join(f"{k}={v}" for k, v in self.target_pod_labels.items())
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector).items
            for pod in pods:
                for cs in pod.status.container_statuses or []:
                    waiting = cs.state.waiting.reason if cs.state.waiting else None
                    terminated = cs.state.terminated.reason if cs.state.terminated else None
                    last = f"{pod.metadata.name}: waiting={waiting} terminated={terminated} restarts={cs.restart_count}"
                    if waiting in unhealthy_reasons or terminated == "OOMKilled" or (cs.restart_count or 0) >= 1:
                        print(f"Fault confirmed live: {last}")
                        return
            print(f"Waiting for squeeze to take effect... {last}")
            time.sleep(5)
        raise RuntimeError(f"Target {self.faulty_service} did not become unhealthy in time; last={last}")

    def _restore_memory_limit(self):
        if self._target_container is None:
            return
        resources = {"limits": {"memory": self._original_memory_limit}}
        if self._original_memory_request is not None:
            resources["requests"] = {"memory": self._original_memory_request}
        patch = {
            "spec": {"template": {"spec": {"containers": [{"name": self._target_container, "resources": resources}]}}}
        }
        self.kubectl.patch_deployment(self.faulty_service, self.namespace, patch)


# ----------------------------------------------------------------------------- priority_preemption_cascade
class PriorityPreemptionCascadeSaleor(PriorityPreemptionCascadeHotelReservation):
    """The unsafe default PriorityClass lets a tenant workload preempt saleor-api.

    saleor-api and saleor-worker share a node-bound volume, so the node cannot be
    chosen; the API pod is still the deterministic victim because every other
    priority-0 pod on its node (StatefulSet pods, the load generator,
    observability) started earlier, and the scheduler reprieves older pods of
    equal priority first. The load generator is left out of the peer
    protection rollout so its ledger survives.
    """

    UNPROTECTED_PEERS = ("loadgen",)

    def __init__(self, faulty_service: str = API, service_name: str = API_SERVICE):
        self.faulty_service = faulty_service
        # PriorityPreemptionMitigationOracle checks this Service's endpoints.
        self.service_name = service_name
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.scheduling_v1 = client.SchedulingV1Api()
        self.target_node = None
        self.target_request_memory = None
        self.pressure_request_memory = None
        self._priority_class_snapshots = {}
        self._deployment_priority_classes = {}
        self._target_original_resources = None
        self._target_original_node_selector = None
        ported(
            self,
            APP,
            component=f"PriorityClass/{self.PLATFORM_PRIORITY_CLASS}",
            description=(
                f"PriorityClass `{self.PLATFORM_PRIORITY_CLASS}` (value 100000) was made the cluster-wide "
                f"`globalDefault`. The production `{faulty_service}` pod (Saleor's GraphQL API) kept priority 0, "
                f"while a new tenant workload `{self.PRESSURE_NAMESPACE}/{self.PRESSURE_DEPLOYMENT}` received the "
                "medium default and a memory request large enough to force scheduler preemption on the API pod's "
                "node. The scheduler evicted the API pod; its replacement inherits the same medium default instead "
                f"of the higher `{self.PRODUCTION_PRIORITY_CLASS}` class, cannot preempt the tenant back and stays "
                f"Pending, so Service `{service_name}` has no endpoints and the API is down even though its image, "
                f"Service and config are valid. Mitigation must make `{self.PLATFORM_PRIORITY_CLASS}` no longer an "
                f"unsafe global default and explicitly protect `{faulty_service}` with a higher-valued production "
                "PriorityClass (without deleting the tenant workload or shrinking requests)."
            ),
            oracle_factory=PriorityPreemptionMitigationOracle,
        )
        self._app_cleanup = self.app.cleanup
        self.app.cleanup = self._cleanup

    def _protect_peer_deployments(self):
        peer_names = [
            deployment.metadata.name
            for deployment in self._app_deployments()
            if deployment.metadata.name not in (self.faulty_service, *self.UNPROTECTED_PEERS)
        ]
        body = {"spec": {"template": {"spec": {"priorityClassName": self.PLATFORM_PRIORITY_CLASS}}}}
        for name in peer_names:
            self.apps_v1.patch_namespaced_deployment(name=name, namespace=self.namespace, body=body)
        for name in peer_names:
            self._wait_for_deployment_ready(name, self.namespace)


# ----------------------------------------------------------------------------- kubelet_crash
class KubeletCrashSaleor(KubeletCrash):
    """Kubelet killed on every worker; saleor-api and saleor-worker are rolled so they cannot come back.

    The original grades with ``AlertOracle``, but the observability stack's
    alert rules are scoped to the original apps' namespaces (no rule matches
    ``saleor``), so it would pass vacuously here. ``MitigationOracle`` checks the
    same state those rules watch (deployments available, pods Ready, nothing
    Pending) directly.
    """

    def __init__(self):
        self.rollout_services = [API, WORKER]
        self.injector = RemoteOSFaultInjector()
        ported(
            self,
            APP,
            component="node/kubelet",
            description=(
                "The kubelet process was killed and stopped on every worker node, so those nodes go NotReady and "
                "no pod lifecycle management happens there. Running containers keep running but their pods are "
                f"marked not Ready (removed from Service endpoints), and the rolled `{API}` and `{WORKER}` pods "
                "cannot be replaced: new pods never start. Saleor's GraphQL API "
                f"(`{API_SERVICE}`) loses its endpoints and Celery work stops; the fix is restarting kubelet on "
                "the nodes."
            ),
            oracle_factory=MitigationOracle,
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_kubelet_crash()
        # Roll the app's own deployments only: a restarted load generator would lose its ledger.
        for service in self.rollout_services:
            self.kubectl.exec_command(f"kubectl rollout restart deployment/{service} -n {self.namespace}")
        self.kubectl.wait_for_ready(self.namespace)


_ORIGINAL_APPS = ("astronomy_shop", "hotel_reservation", "social_network")

PORTS: dict[str, tuple[str, type]] = {
    **{
        f"liveness_probe_too_aggressive_{app}": (
            "liveness_probe_too_aggressive_saleor",
            LivenessProbeTooAggressiveSaleor,
        )
        for app in _ORIGINAL_APPS
    },
    **{
        f"sidecar_port_conflict_{app}": ("sidecar_port_conflict_saleor", SidecarPortConflictSaleor)
        for app in _ORIGINAL_APPS
    },
    "assign_to_non_existent_node": ("assign_to_non_existent_node_saleor", AssignNonExistentNodeSaleor),
    "wrong_bin_usage": ("wrong_bin_usage_saleor", WrongBinUsageSaleor),
    "priority_preemption_cascade_hotel_reservation": (
        "priority_preemption_cascade_saleor",
        PriorityPreemptionCascadeSaleor,
    ),
    "pvc_claim_mismatch": ("pvc_claim_mismatch_saleor", PVCClaimMismatchSaleor),
    "rbac_misconfiguration": ("rbac_misconfiguration_saleor", RBACMisconfigurationSaleor),
    "resource_request_too_small": ("resource_request_too_small_saleor", ResourceRequestTooSmallSaleor),
    "nightly_rebalance_oom_hotel_reservation": ("nightly_rebalance_oom_saleor", NightlyRebalanceOOMSaleor),
    "kubelet_crash": ("kubelet_crash_saleor", KubeletCrashSaleor),
}
