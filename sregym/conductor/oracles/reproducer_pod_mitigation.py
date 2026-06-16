"""Mitigation oracle for auto-generated problems with a continuous reproducer.

Checks the readiness state of the reproducer Deployment's pods:

  - Wrong-result bugs (expected_output set): the readiness probe greps for the
    buggy value, so pod Ready = bug present, NotReady = bug fixed.
  - Crash/error bugs (no expected_output): the readiness probe runs the query
    and checks the exit code, so pod NotReady = bug present, Ready = bug fixed.
"""

import logging
import time

from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)

_SETTLE_TIMEOUT_S = 30
_POLL_INTERVAL_S = 3


class ReproducerPodMitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, cluster_name: str, expect_unready: bool = True):
        """
        Args:
            cluster_name: used to derive the deployment name ``{cluster_name}-reproducer``.
            expect_unready: True for wrong-result bugs (NotReady = fixed).
                            False for crash/error bugs (Ready = fixed).
        """
        super().__init__(problem)
        self.deployment_name = f"{cluster_name}-reproducer"
        self.expect_unready = expect_unready

    def evaluate(self) -> dict:
        from sregym.service.kubectl import KubeCtl

        kubectl = KubeCtl()
        namespace = self.problem.namespace

        logger.info(
            f"[ReproducerMitigation] Checking deployment '{self.deployment_name}' "
            f"in '{namespace}' (expect_unready={self.expect_unready})"
        )

        try:
            kubectl.get_deployment(self.deployment_name, namespace)
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"[ReproducerMitigation] Deployment '{self.deployment_name}' not found")
                return {"success": False, "reason": "reproducer deployment not found"}
            raise

        deadline = time.time() + _SETTLE_TIMEOUT_S
        last_reason = ""
        while time.time() < deadline:
            passed, reason = self._check(kubectl, namespace)
            last_reason = reason
            if passed:
                logger.info(f"[ReproducerMitigation] PASS — {reason}")
                return {"success": True, "reason": reason}
            time.sleep(_POLL_INTERVAL_S)

        logger.info(f"[ReproducerMitigation] FAIL — {last_reason}")
        return {"success": False, "reason": last_reason}

    def _check(self, kubectl, namespace: str) -> tuple[bool, str]:
        pods = kubectl.core_v1_api.list_namespaced_pod(
            namespace, label_selector=f"app={self.deployment_name}"
        )
        if not pods.items:
            return False, "no reproducer pods found"

        for pod in pods.items:
            if not pod.status or not pod.status.container_statuses:
                return False, f"pod {pod.metadata.name} has no container statuses yet"

            for cs in pod.status.container_statuses:
                is_ready = bool(cs.ready)
                if self.expect_unready and is_ready:
                    return False, f"container {cs.name} is still Ready (bug still present)"
                if not self.expect_unready and not is_ready:
                    return False, f"container {cs.name} is still NotReady (bug still present)"

        state = "NotReady" if self.expect_unready else "Ready"
        return True, f"all reproducer containers are {state} — mitigation confirmed"
