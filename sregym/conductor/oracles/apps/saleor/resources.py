"""Saleor: resources."""

from __future__ import annotations

import time

from sregym.conductor.oracles.fd_exhaustion import FDMitigationOracle
from sregym.conductor.oracles.namespace_memory_limit_mitigation import NamespaceMemoryLimitMitigationOracle


class SaleorFDMitigationOracle(FDMitigationOracle):
    """``FDMitigationOracle`` plus the open-file limit the API processes actually run with.

    The log check alone samples one pod's last lines; the limit is the fault's
    state, read from ``/proc/<pid>/limits`` of every process in every API pod.
    """

    MIN_OPEN_FILES = 16384

    def evaluate(self) -> dict:
        result = super().evaluate()
        if not result.get("success"):
            return result
        problem = self.problem
        selector = problem.api_selector()
        pods = problem.app.pod_names(selector)
        if not pods:
            return self.fail("fault_still_present", detail="no running API pod")
        script = (
            "for p in /proc/[0-9]*; do [ -r $p/limits ] && "
            "awk '/Max open files/ {print $4}' $p/limits; done | sort -n | head -1"
        )
        for pod in pods:
            try:
                low = problem.app.exec_in(f"pod/{pod}", script).strip()
                limit = float("inf") if low in ("", "unlimited") else int(low)
            except Exception as exc:
                return self.fail_from_exception(exc, pod=pod)
            if limit < self.MIN_OPEN_FILES:
                print(f"❌ {pod} runs with an open-file limit of {limit} (< {self.MIN_OPEN_FILES})")
                return self.fail("fault_still_present", pod=pod, nofile=limit)
        print(f"✅ Every {problem.faulty_service} process may open >= {self.MIN_OPEN_FILES} files")
        return {"success": True}


class StatefulSetMemoryLimitMitigationOracle(NamespaceMemoryLimitMitigationOracle):
    """``NamespaceMemoryLimitMitigationOracle`` for a StatefulSet target."""

    def _statefulset_ready(self):
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            sts = self.problem.kubectl.apps_v1_api.read_namespaced_stateful_set(
                self.problem.faulty_service, self.problem.namespace
            )
            desired = 1 if sts.spec.replicas is None else sts.spec.replicas
            status = sts.status
            if (
                desired >= 1
                and (status.ready_replicas or 0) == desired
                and (status.updated_replicas or 0) == desired
                and (status.observed_generation or 0) >= (sts.metadata.generation or 0)
            ):
                return sts
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.poll_interval_seconds)

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Namespace Memory Limit Mitigation Evaluation ==")
        name = self.problem.faulty_service
        try:
            active_memory_quotas = self._active_memory_quotas()
            if active_memory_quotas:
                print(f"[FAIL] Namespace-wide memory admission requirements remain: {active_memory_quotas}")
                return self.fail("fault_still_present", quotas=sorted(active_memory_quotas))
            sts = self._statefulset_ready()
            if sts is None:
                print(f"[FAIL] StatefulSet '{name}' is not fully Ready")
                return self.fail("required_statefulset_not_ready", statefulset=name)
            if not self._service_has_ready_target_endpoint(sts):
                return self.fail("no_ready_endpoints", service=name)
            if not self._run_fresh_admission_and_connection_probe():
                print(f"[FAIL] A fresh no-memory-resource pod could not connect to {name}")
                return self.fail("fresh_pod_admission_failed", namespace=self.problem.namespace)
        except Exception as exc:
            print(f"[FAIL] Error checking namespace memory mitigation: {exc}")
            return self.fail_from_exception(exc)
        print(f"[PASS] Namespace memory admission is safe and {name} is reachable")
        return {"success": True}
