"""Mitigation oracle for the 'memory limit too low -> OOMKilled' problem."""

import logging

from sregym.conductor.oracles.base import Oracle


class MemoryLimitOOMKillMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem=problem)
        self.logger = logging.getLogger(__name__)

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (memory-limit OOMKill) ==")
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        dep_name = self.problem.deployment_name

        # 1) Deployment must still exist -> guards against "delete the workload".
        try:
            dep = kubectl.get_deployment(dep_name, namespace)
        except Exception as e:
            print(f"[FAIL] Target deployment {dep_name} not found: {e}")
            return {"success": False}

        # 2) Replicas must not be scaled below the original desired count
        #    -> guards against "scale to zero so nothing can be unhealthy".
        desired = dep.spec.replicas or 0
        if desired < self.problem.original_replicas:
            print(f"[FAIL] {dep_name} scaled to {desired} replicas (original was {self.problem.original_replicas}).")
            return {"success": False}

        # 3) Find this deployment's pods via its selector labels.
        match_labels = dep.spec.selector.match_labels or {}
        pods = [
            p
            for p in kubectl.list_pods(namespace).items
            if all((p.metadata.labels or {}).get(k) == v for k, v in match_labels.items())
        ]
        if not pods:
            print(f"[FAIL] No pods found for deployment {dep_name}.")
            return {"success": False}

        # 4) Every target pod must be Running, Ready, and not crashing/OOMing now.
        #    (We intentionally do NOT fail on last_state==OOMKilled: a freshly
        #     fixed pod whose *previous* generation OOMed but is now Running+Ready
        #     is exactly the success state.)
        bad_waiting = {"CrashLoopBackOff", "OOMKilled", "Error"}
        for pod in pods:
            if pod.status.phase != "Running":
                print(f"[FAIL] Pod {pod.metadata.name} phase={pod.status.phase}")
                return {"success": False}
            for cs in pod.status.container_statuses or []:
                if not cs.ready:
                    print(f"[FAIL] Container {cs.name} in {pod.metadata.name} not ready")
                    return {"success": False}
                waiting = cs.state.waiting if cs.state else None
                if waiting and waiting.reason in bad_waiting:
                    print(f"[FAIL] Container {cs.name} waiting: {waiting.reason}")
                    return {"success": False}

        print("[OK] Target workload exists, is at full scale, and is no longer OOMKilling.")
        return {"success": True}
