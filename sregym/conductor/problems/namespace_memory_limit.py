import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.namespace_memory_limit_mitigation import NamespaceMemoryLimitMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class NamespaceMemoryLimit(Problem):
    QUOTA_NAME = "memory-limit-quota"
    MEMORY_QUOTA_KEYS = frozenset({"memory", "requests.memory", "limits.memory"})
    MEMORY_LIMIT = "1Gi"
    rollout_timeout_seconds = 120
    fault_timeout_seconds = 120
    poll_interval_seconds = 2

    def __init__(self):
        super().__init__(app=HotelReservation())
        self.kubectl = KubeCtl()
        self.faulty_service = "search"
        self.root_cause = self.build_structured_root_cause(
            component=f"resourcequota/{self.QUOTA_NAME}",
            namespace=self.namespace,
            description=(
                f"Namespace-wide ResourceQuota `{self.QUOTA_NAME}` enforces memory declarations, but the existing "
                f"workloads do not declare them. Recreating deployment `{self.faulty_service}` exposes the problem "
                "immediately: admission rejects its replacement pod with `must specify memory`. Other noncompliant "
                "workloads are also vulnerable on restart even though their existing pods continue running."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = NamespaceMemoryLimitMitigationOracle(problem=self)

        self.app.create_workload()
        self._baseline_replicas = None

    @staticmethod
    def _desired_replicas(deployment) -> int:
        replicas = deployment.spec.replicas
        return 1 if replicas is None else replicas

    @staticmethod
    def _quota_hard_keys(quota) -> set[str]:
        return set(quota.spec.hard or {})

    @classmethod
    def _rollout_at_replicas(cls, deployment, expected: int) -> bool:
        if cls._desired_replicas(deployment) != expected:
            return False

        status = deployment.status
        generation = deployment.metadata.generation or 0
        return (
            (status.observed_generation or 0) >= generation
            and (status.replicas or 0) == expected
            and (status.updated_replicas or 0) == expected
            and (status.ready_replicas or 0) == expected
            and (status.available_replicas or 0) == expected
            and (status.unavailable_replicas or 0) == 0
        )

    def _wait_for_rollout(self, expected_replicas: int):
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
            if self._rollout_at_replicas(deployment, expected_replicas):
                return deployment
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Deployment '{self.faulty_service}' did not become ready at {expected_replicas} replicas"
                )
            time.sleep(self.poll_interval_seconds)

    def _wait_for_fault_symptom(self):
        deadline = time.monotonic() + self.fault_timeout_seconds
        while True:
            deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
            desired = self._desired_replicas(deployment)
            if (deployment.status.ready_replicas or 0) < desired:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Deployment '{self.faulty_service}' remained ready after quota injection")
            time.sleep(self.poll_interval_seconds)

    @staticmethod
    def _pod_matches_selector(pod, selector: dict[str, str]) -> bool:
        labels = pod.metadata.labels or {}
        return all(labels.get(key) == value for key, value in selector.items())

    def _wait_for_ready_endpoint(self, deployment):
        selector = deployment.spec.selector.match_labels or {}
        if not selector:
            raise RuntimeError(f"Deployment '{self.faulty_service}' has no matchLabels selector")

        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            target_pods = {
                pod.metadata.name
                for pod in self.kubectl.list_pods(self.namespace).items
                if self._pod_matches_selector(pod, selector)
            }
            endpoints = self.kubectl.core_v1_api.read_namespaced_endpoints(
                name=self.faulty_service,
                namespace=self.namespace,
            )
            ready_targets = {
                address.target_ref.name
                for subset in endpoints.subsets or []
                for address in subset.addresses or []
                if address.target_ref is not None
                and address.target_ref.kind == "Pod"
                and address.target_ref.name in target_pods
            }
            if ready_targets:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Service '{self.faulty_service}' did not regain a ready target endpoint")
            time.sleep(self.poll_interval_seconds)

    def _validate_injection_baseline(self, deployment):
        desired = self._desired_replicas(deployment)
        if desired < 1:
            raise RuntimeError(f"Deployment '{self.faulty_service}' must have at least one desired replica")

        quotas = self.kubectl.get_resource_quotas(self.namespace)
        if any(quota.metadata.name == self.QUOTA_NAME for quota in quotas):
            raise RuntimeError(f"ResourceQuota '{self.QUOTA_NAME}' already exists")

        active_memory_quotas = [
            quota.metadata.name for quota in quotas if self._quota_hard_keys(quota) & self.MEMORY_QUOTA_KEYS
        ]
        if active_memory_quotas:
            names = ", ".join(sorted(active_memory_quotas))
            raise RuntimeError(f"Cannot inject over existing memory ResourceQuota: {names}")

        missing_memory = any(
            container.resources is None or "memory" not in (container.resources.requests or {})
            for container in deployment.spec.template.spec.containers
        )
        if not missing_memory:
            raise RuntimeError(f"Deployment '{self.faulty_service}' already declares memory requests")

        replica_sets = self.kubectl.get_matching_replicasets(self.namespace, self.faulty_service)
        active_replica_sets = [
            replica_set
            for replica_set in replica_sets
            if (replica_set.spec.replicas or 0) > 0
            or (replica_set.status is not None and (replica_set.status.replicas or 0) > 0)
        ]
        if not active_replica_sets:
            raise RuntimeError(f"No active ReplicaSet found for deployment {self.faulty_service} in {self.namespace}")
        return desired, active_replica_sets

    def _create_memory_quota(self):
        self.kubectl.apply_resource(
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {
                    "name": self.QUOTA_NAME,
                    "namespace": self.namespace,
                },
                "spec": {"hard": {"memory": self.MEMORY_LIMIT}},
            }
        )

    def _delete_injected_quota(self):
        quotas = self.kubectl.get_resource_quotas(self.namespace)
        if any(quota.metadata.name == self.QUOTA_NAME for quota in quotas):
            self.kubectl.delete_resource_quota(name=self.QUOTA_NAME, namespace=self.namespace)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        desired, active_replica_sets = self._validate_injection_baseline(deployment)
        self._baseline_replicas = desired
        self._create_memory_quota()
        for replica_set in active_replica_sets:
            self.kubectl.delete_replicaset(name=replica_set.metadata.name, namespace=self.namespace)
        self._wait_for_fault_symptom()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        replicas = self._baseline_replicas
        if replicas is None:
            replicas = self._desired_replicas(deployment)
        if replicas < 1:
            raise RuntimeError("Cannot recover the quota fault without a positive baseline replica count")

        self._delete_injected_quota()
        self.kubectl.scale_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            replicas=0,
        )
        self._wait_for_rollout(0)
        self.kubectl.scale_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            replicas=replicas,
        )
        deployment = self._wait_for_rollout(replicas)
        self._wait_for_ready_endpoint(deployment)
        self._baseline_replicas = None
