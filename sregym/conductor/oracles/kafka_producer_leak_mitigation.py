"""Require a stable Kafka broker after the producer workload is mitigated."""

import time

from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.mitigation import MitigationOracle


class KafkaProducerLeakOracle(MitigationOracle):
    STABILITY_SECONDS = 600
    POLL_SECONDS = 5
    ROLLOUT_SECONDS = 300
    # Only this oracle needs the longer budget. Allow for rollout, observation,
    # and API overhead without changing agent or cleanup timeouts.
    evaluation_timeout_seconds = ROLLOUT_SECONDS + STABILITY_SECONDS + 60

    def capture_baseline(self) -> None:
        super().capture_baseline()
        deployment = self.problem.kubectl.get_deployment("kafka", self.problem.namespace)
        container = next(c for c in deployment.spec.template.spec.containers if c.name == "kafka")
        self.problem.heap_limit = self._heap(container)
        self.problem.memory_limit = self._memory(container)

    @staticmethod
    def _heap(container):
        values = [e.value for e in (container.env or []) if e.name == "KAFKA_HEAP_OPTS"]
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _memory(container):
        return (container.resources.limits or {}).get("memory") if container.resources else None

    def _check_resources(self, broker) -> None:
        heap = getattr(self.problem, "heap_limit", None)
        memory = getattr(self.problem, "memory_limit", None)
        if broker is None or not heap or not memory or self._heap(broker) != heap or self._memory(broker) != memory:
            raise ValueError("Kafka heap or memory limit differs from the pre-injection baseline")

    def _wait_for_rollouts(self, kubectl, namespace):
        deadline = time.monotonic() + self.rollout_time
        super()._wait_for_rollouts(kubectl, namespace)
        # Deployment counters can already report success while replaced pods
        # are still terminating. Include that drain in the same initial grace
        # period, before requiring a fixed set of containers for observation.
        while True:
            draining = False
            for name in (self.problem.faulty_service, "kafka"):
                deployment = kubectl.get_deployment(name, namespace)
                pods = kubectl.get_deployment_pods(deployment, namespace)
                desired = deployment.spec.replicas or 0
                if desired < 1:
                    return
                if any(p.metadata.deletion_timestamp for p in pods) or len(pods) > desired:
                    draining = True
            remaining = deadline - time.monotonic()
            if not draining or remaining <= 0:
                return
            time.sleep(min(self.POLL_SECONDS, remaining))

    def _snapshot(self) -> dict:
        """Validate the current rollout and identify the controlled containers."""
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        snapshot = {}
        for name in (self.problem.faulty_service, "kafka"):
            deployment = kubectl.get_deployment(name, namespace)
            status = deployment.status
            desired = deployment.spec.replicas or 0
            if (
                desired < 1
                or not status
                or (status.observed_generation or 0) < deployment.metadata.generation
                or (status.updated_replicas or 0) != desired
                or (status.ready_replicas or 0) != desired
                or (status.available_replicas or 0) != desired
            ):
                raise ValueError(f"{name} does not have a complete Ready rollout")
            if name == "kafka":
                self._check_resources(
                    next((c for c in deployment.spec.template.spec.containers if c.name == "kafka"), None)
                )
            pods = kubectl.get_deployment_pods(deployment, namespace)
            if len(pods) != desired or any(p.metadata.deletion_timestamp for p in pods):
                raise ValueError(f"{name} does not have a stable set of controlled pods")
            snapshot[name] = (deployment.metadata.uid, deployment.metadata.generation)
            for pod in pods:
                statuses = pod.status.container_statuses or []
                if (
                    pod.status.phase != "Running"
                    or len(statuses) != len(pod.spec.containers)
                    or not statuses
                    or not all(c.ready for c in statuses)
                ):
                    raise ValueError(f"{name} has a pod that is not Ready")
                if name == "kafka":
                    self._check_resources(next((c for c in pod.spec.containers if c.name == "kafka"), None))
                snapshot[(name, pod.metadata.uid)] = tuple(
                    sorted((c.name, c.restart_count, c.container_id) for c in statuses)
                )
        return snapshot

    def evaluate(self) -> dict:
        self.rollout_time = self.ROLLOUT_SECONDS
        started = time.monotonic()
        try:
            result = super().evaluate()
            if not result["success"]:
                return result
            initial = self._snapshot()
            deadline = time.monotonic() + self.STABILITY_SECONDS
            while True:
                result = self._evaluate_current_state()
                if not result["success"]:
                    return result
                if self._snapshot() != initial:
                    raise ValueError("A required workload changed or restarted during observation")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"success": True, "stable_seconds": self.STABILITY_SECONDS}
                time.sleep(min(self.POLL_SECONDS, remaining))
        except (ApiException, ValueError) as exc:
            return {"success": False, "reason": str(exc), "observed_seconds": round(time.monotonic() - started, 2)}
