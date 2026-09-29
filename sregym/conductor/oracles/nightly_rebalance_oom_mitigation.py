"""Mitigation oracle for NightlyRebalanceOOM."""

import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

# Used only when the target had no explicit memory limit originally.
_FALLBACK_FLOOR_BYTES = 64 * 1024 * 1024  # 64Mi


def _parse_mem_to_bytes(value: str) -> int:
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "k": 1000, "M": 1000**2, "G": 1000**3}
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * factor)
    return int(value)


class NightlyRebalanceOOMMitigationOracle(MitigationOracle):
    importance = 1.0

    FAILURE_CLASSES = {
        # Leaving the rebalancer free to re-apply the unsafe limit is the
        # difference between a fix and a temporary patch, and it is the agent's
        # to get right -- the CronJob is part of the injected scenario.
        "fix_reverted_by_actor": FailureClass.AGENT_ERROR,
    }

    def evaluate(self) -> dict:
        print("== Nightly Rebalance OOM Mitigation Evaluation ==")
        results = super().evaluate()
        if not results.get("success"):
            return results

        service = self.problem.faulty_service
        namespace = self.problem.namespace

        if not self._memory_limit_sane(service, namespace):
            print(f"❌ Memory limit on deployment/{service} is still below the restored baseline")
            # Compared against a baseline captured before injection.
            return self.fail("fault_still_present", deployment=service)

        if not self._target_pods_healthy(service, namespace):
            print(f"❌ Pods for {service} are not all running/ready without recent OOMKills")
            return self.fail("pods_not_ready", deployment=service, namespace=namespace)

        if self._rebalancer_active() and not self._survives_next_tick(service, namespace):
            print(f"❌ {self.problem.actor_name} CronJob still active and re-applies an unsafe limit")
            # The limit was restored but the CronJob puts it back: a fix that
            # does not survive the next tick, which is the point of the problem.
            return self.fail("fix_reverted_by_actor", actor=self.problem.actor_name, deployment=service)

        print("✅ Memory limit restored and target service durably healthy")
        return {"success": True}

    def _rebalancer_active(self) -> bool:
        try:
            cj = client.BatchV1Api().read_namespaced_cron_job(self.problem.actor_name, self.problem.actor_namespace)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        return not bool(cj.spec.suspend)

    def _survives_next_tick(self, service: str, namespace: str, timeout: int = 150, poll: int = 5) -> bool:
        batch = client.BatchV1Api()
        actor_ns = self.problem.actor_namespace
        label = f"app={self.problem.actor_name}"
        baseline = {j.metadata.name for j in batch.list_namespaced_job(actor_ns, label_selector=label).items}
        deadline = time.monotonic() + timeout
        saw_tick = False
        while time.monotonic() < deadline:
            fresh = [
                j
                for j in batch.list_namespaced_job(actor_ns, label_selector=label).items
                if j.metadata.name not in baseline
            ]
            if any((j.status.succeeded or 0) or (j.status.failed or 0) for j in fresh):
                saw_tick = True
                break
            time.sleep(poll)
        return (
            saw_tick and self._memory_limit_sane(service, namespace) and self._target_pods_healthy(service, namespace)
        )

    def _memory_limit_sane(self, service: str, namespace: str) -> bool:
        original = getattr(self.problem, "_original_memory_limit", None)
        target = getattr(self.problem, "_target_container", None)
        dep = self.problem.kubectl.get_deployment(service, namespace)
        for container in dep.spec.template.spec.containers:
            if target is not None and container.name != target:
                continue
            limits = (container.resources.limits or {}) if container.resources else {}
            mem = limits.get("memory")
            if original is not None:
                if mem is None or _parse_mem_to_bytes(mem) < _parse_mem_to_bytes(original):
                    return False
            elif mem is not None and _parse_mem_to_bytes(mem) < _FALLBACK_FLOOR_BYTES:
                return False
        return True

    def _target_pods_healthy(self, service: str, namespace: str) -> bool:
        pods = self.problem.kubectl.list_pods(namespace).items
        target = [p for p in pods if (p.metadata.labels or {}).get("io.kompose.service") == service]
        if not target:
            return False
        for pod in target:
            if pod.status.phase != "Running":
                return False
            for cs in pod.status.container_statuses or []:
                if not cs.ready:
                    return False
                last = cs.last_state.terminated if cs.last_state else None
                if last and last.reason == "OOMKilled":
                    return False
        return True
