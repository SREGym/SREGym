"""LimitRange silent defaulting causes ResourceQuota exhaustion on Hotel Reservation.

A platform team applies a ``LimitRange`` to inject default memory requests into
pods that omit them, paired with a ``ResourceQuota`` to cap aggregate namespace
usage. The Hotel Reservation pods ship without explicit memory requests (only CPU),
so the LimitRange silently injects defaults into every new pod. With all pods
running under these injected defaults, the aggregate memory usage fully saturates
the quota. When a pod is deleted (rolling update, eviction, crash), the
ReplicaSet's create call is rejected by the API server with ``exceeded quota``,
even though no individual pod looks oversized.

The failure is deceptive: ``kubectl get pods`` shows healthy Running pods, no pod
spec declares large requests, and the quota error only surfaces in ReplicaSet
events. The agent must connect three objects — the LimitRange (injecting invisible
defaults), the ResourceQuota (capping aggregate), and the ReplicaSet FailedCreate
events — to diagnose the root cause.

Real-world references:
- https://kubernetes.io/docs/concepts/policy/limit-range/
- https://github.com/kubernetes/kubernetes/issues/67814
- Common in multi-tenant GKE/EKS clusters when platform teams enforce governance
  policies without auditing existing workloads' resource declarations.
"""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.deployment_readiness import DeploymentReadinessOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Names chosen to look like standard platform governance objects — no SREGym
# or fault-specific naming that could leak benchmark metadata to the agent.
LIMITRANGE_NAME = "default-mem-limits"
RESOURCEQUOTA_NAME = "mem-quota"

# Default memory injected per container by the LimitRange.
DEFAULT_MEM_REQUEST = "128Mi"
DEFAULT_MEM_LIMIT = "256Mi"


class LimitRangeQuotaExhaustionHotelReservation(Problem):
    """Inject a LimitRange + ResourceQuota pair that blocks pod recreation
    after the silently injected memory defaults exhaust the namespace quota."""

    def __init__(self):
        super().__init__(app=HotelReservation())

        self.kubectl = KubeCtl()
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.faulty_service = "recommendation"

        self.root_cause = self.build_structured_root_cause(
            component=f"LimitRange/{LIMITRANGE_NAME} + ResourceQuota/{RESOURCEQUOTA_NAME}",
            namespace=self.namespace,
            description=(
                f"A LimitRange '{LIMITRANGE_NAME}' in the '{self.namespace}' namespace silently "
                f"injects default memory requests ({DEFAULT_MEM_REQUEST}) and limits "
                f"({DEFAULT_MEM_LIMIT}) into every container that omits explicit memory "
                "resource specifications. The Hotel Reservation pods do not declare memory "
                f"requests, so every pod created after the LimitRange was applied receives "
                f"these hidden defaults. A ResourceQuota '{RESOURCEQUOTA_NAME}' caps "
                "aggregate namespace memory to a value that fits all currently running pods "
                "but has no headroom for a replacement. When the "
                f"'{self.faulty_service}' pod is deleted, the ReplicaSet's recreate is "
                "rejected by the API server with 'exceeded quota' because the injected "
                "defaults push aggregate memory usage over the quota limit. Mitigation: "
                "delete or increase the ResourceQuota, or remove the LimitRange's default "
                "memory injection, then allow the ReplicaSet to recreate the pod."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = DeploymentReadinessOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        # 1. Create LimitRange that injects default memory requests/limits.
        #    This only affects pods created AFTER this point — already-running
        #    pods are untouched (LimitRange only gates admission).
        limit_range = client.V1LimitRange(
            metadata=client.V1ObjectMeta(name=LIMITRANGE_NAME),
            spec=client.V1LimitRangeSpec(
                limits=[
                    client.V1LimitRangeItem(
                        type="Container",
                        default={"memory": DEFAULT_MEM_LIMIT},
                        default_request={"memory": DEFAULT_MEM_REQUEST},
                    )
                ]
            ),
        )
        self._create_or_replace_limit_range(limit_range)
        print(
            f"Created LimitRange '{LIMITRANGE_NAME}': "
            f"default memory request={DEFAULT_MEM_REQUEST}, limit={DEFAULT_MEM_LIMIT}"
        )

        # 2. Count total container instances across all deployments to compute
        #    a tight quota. Each container will get 128Mi injected by the
        #    LimitRange when recreated. We set the quota so that N-1 pods fit
        #    but the Nth (replacement) pod doesn't.
        deployments = self.apps_v1.list_namespaced_deployment(self.namespace)
        total_containers = 0
        for dep in deployments.items:
            replicas = dep.spec.replicas or 1
            n_containers = len(dep.spec.template.spec.containers)
            total_containers += replicas * n_containers

        # Trigger rolling restart so ALL pods pick up the LimitRange defaults.
        # We patch a governance annotation into the pod template to force this.
        restart_annotation = {
            "spec": {"template": {"metadata": {"annotations": {"governance/policy-applied": "true"}}}}
        }
        for dep in deployments.items:
            self.apps_v1.patch_namespaced_deployment(dep.metadata.name, self.namespace, restart_annotation)
        print(f"Triggered rolling restart of {len(deployments.items)} deployments")

        # 3. Wait for ALL deployments to finish their rolling restart so every
        #    pod now has the LimitRange-injected memory defaults.
        self._wait_for_all_deployments_ready(timeout=600)

        # 4. Set ResourceQuota to exactly fit N-1 pods.
        #    quota = (total_containers - 1) × DEFAULT_MEM_REQUEST (128Mi each).
        #    When a pod is deleted, the usage drops to exactly the quota limit.
        #    The replacement CANNOT fit because creating it would exceed the quota.
        default_mem_bytes = self._parse_memory(DEFAULT_MEM_REQUEST)
        quota_bytes = (total_containers - 1) * default_mem_bytes
        quota_mem = f"{quota_bytes // (1024 * 1024)}Mi"
        print(f"Computed tight quota: ({total_containers}-1) containers × {DEFAULT_MEM_REQUEST} = {quota_mem}")

        resource_quota = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name=RESOURCEQUOTA_NAME),
            spec=client.V1ResourceQuotaSpec(hard={"requests.memory": quota_mem}),
        )
        self._create_or_replace_resource_quota(resource_quota)
        print(f"Created ResourceQuota '{RESOURCEQUOTA_NAME}': requests.memory={quota_mem}")

        # 5. Delete one pod of the target deployment. The ReplicaSet will try
        #    to recreate it, but the quota is fully saturated — FailedCreate.
        pods = self.core_v1.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=f"io.kompose.service={self.faulty_service}",
        )
        if not pods.items:
            raise RuntimeError(f"No pods found for service '{self.faulty_service}' in namespace '{self.namespace}'")
        target_pod = pods.items[0].metadata.name
        self.core_v1.delete_namespaced_pod(
            name=target_pod,
            namespace=self.namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0),
        )
        print(f"Deleted pod {target_pod}")

        # 6. Wait briefly for the FailedCreate events to appear.
        self._wait_for_failed_create(timeout=60)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # Remove both governance objects. The ReplicaSet will automatically
        # recreate the missing pod once the quota constraint is gone.
        with contextlib.suppress(ApiException):
            self.core_v1.delete_namespaced_resource_quota(RESOURCEQUOTA_NAME, self.namespace)
        print(f"Deleted ResourceQuota '{RESOURCEQUOTA_NAME}'")

        with contextlib.suppress(ApiException):
            self.core_v1.delete_namespaced_limit_range(LIMITRANGE_NAME, self.namespace)
        print(f"Deleted LimitRange '{LIMITRANGE_NAME}'")

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _create_or_replace_limit_range(self, lr: client.V1LimitRange):
        try:
            self.core_v1.create_namespaced_limit_range(self.namespace, lr)
        except ApiException as e:
            if e.status == 409:
                self.core_v1.replace_namespaced_limit_range(LIMITRANGE_NAME, self.namespace, lr)
            else:
                raise

    def _create_or_replace_resource_quota(self, rq: client.V1ResourceQuota):
        try:
            self.core_v1.create_namespaced_resource_quota(self.namespace, rq)
        except ApiException as e:
            if e.status == 409:
                self.core_v1.replace_namespaced_resource_quota(RESOURCEQUOTA_NAME, self.namespace, rq)
            else:
                raise

    def _sum_pod_memory_requests(self) -> int:
        """Return the total memory requests (in bytes) of all running pods."""
        pods = self.core_v1.list_namespaced_pod(self.namespace)
        total = 0
        for pod in pods.items:
            if pod.status.phase != "Running":
                continue
            for container in pod.spec.containers:
                requests = (container.resources.requests or {}) if container.resources else {}
                mem_str = requests.get("memory", "0")
                total += self._parse_memory(mem_str)
        return total

    @staticmethod
    def _parse_memory(mem_str: str) -> int:
        """Parse a Kubernetes memory string (e.g. '128Mi', '1Gi') to bytes."""
        if not mem_str or mem_str == "0":
            return 0
        mem_str = str(mem_str)
        units = {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "K": 1000,
            "M": 1000**2,
            "G": 1000**3,
            "T": 1000**4,
        }
        for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
            if mem_str.endswith(suffix):
                return int(mem_str[: -len(suffix)]) * multiplier
        # Plain integer = bytes
        return int(mem_str)

    def _wait_for_all_deployments_ready(self, timeout: int = 300):
        """Block until every deployment in the namespace has all replicas ready and rollout is complete."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            deployments = self.apps_v1.list_namespaced_deployment(self.namespace)
            all_ready = True
            for dep in deployments.items:
                desired = dep.spec.replicas or 1
                status = dep.status
                if (
                    status.observed_generation != dep.metadata.generation
                    or (status.updated_replicas or 0) < desired
                    or (status.replicas or 0) > desired
                    or (status.available_replicas or 0) < desired
                ):
                    all_ready = False
                    break
            if all_ready:
                print("All deployments fully rolled out and ready.")
                return
            time.sleep(5)
        print("⚠️ Timed out waiting for deployments to stabilize; proceeding.")

    def _wait_for_failed_create(self, timeout: int = 60):
        """Best-effort wait for the FailedCreate event to surface on the ReplicaSet."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.core_v1.list_namespaced_event(self.namespace)
            for event in events.items:
                if event.reason == "FailedCreate" and "exceeded quota" in (event.message or "").lower():
                    print(f"FailedCreate event confirmed: {event.message[:120]}")
                    return
            time.sleep(3)
        print("⚠️ FailedCreate event not seen within timeout; fault may still be active.")
