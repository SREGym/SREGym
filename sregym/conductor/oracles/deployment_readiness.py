"""Mitigation oracle for problems where the fault prevents pods of a
specific deployment from being created at all.

When the fault category prevents pod admission or creation (admission webhooks,
scheduling deadlocks, PDB-blocked drains, etc.) the affected pod is simply
absent from the namespace's pod list, so a per-pod health walk reports success
because the remaining pods are healthy.

This oracle reads the Deployment object directly and verifies its
``ready_replicas`` matches ``spec.replicas`` — which works regardless of
whether the missing pod is Pending, CrashLooping, or completely absent.
It also walks the rest of the namespace's pods to catch crashloops or other
downstream breakage the agent may have introduced.
"""

import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class DeploymentReadinessOracle(Oracle):
    importance = 1.0

    # This oracle deliberately separates a primary from a secondary check, and
    # the classification follows that split rather than flattening it.
    #
    # The primary check is the fault signature: this oracle exists for faults
    # that stop the affected Deployment's pods being created at all (admission
    # webhooks, scheduling deadlocks, PDB-blocked drains). If that Deployment
    # is still short of ready replicas after the settle window, the fault it
    # was asked to clear is still in effect.
    #
    # The secondary walk over the rest of the namespace is looking for
    # collateral damage, and stays ambiguous: an unrelated pod that will not
    # run may be the agent's doing or the cluster's.
    FAILURE_CLASSES = {
        "faulty_deployment_not_ready": FailureClass.AGENT_ERROR,
    }

    def _wait_for_rollouts(self, kubectl, namespace):
        """Wait for all deployments in the namespace to finish rolling out so we
        evaluate stable state rather than a transient window."""
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = True
            for dep in deployments.items:
                status = dep.status
                desired = dep.spec.replicas or 1
                if (
                    (status.updated_replicas or 0) < desired
                    or (status.ready_replicas or 0) < desired
                    or (status.unavailable_replicas or 0) > 0
                ):
                    all_settled = False
                    break
            if all_settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print("⚠️ Timed out waiting for deployments to settle; evaluating current state")

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_name = self.problem.faulty_service

        self._wait_for_rollouts(kubectl, namespace)

        # Primary check: the affected deployment has all desired replicas ready.
        # This is the check that the generic MitigationOracle's per-pod walk
        # would miss when the fault prevents pod creation entirely.
        deployment = kubectl.get_deployment(deployment_name, namespace)
        if deployment is None:
            print(f"❌ Deployment '{deployment_name}' not found in namespace '{namespace}'")
            return self.fail("required_deployment_missing", deployment=deployment_name, namespace=namespace)

        desired = deployment.spec.replicas or 0
        ready = deployment.status.ready_replicas or 0
        if ready != desired:
            print(f"❌ Deployment '{deployment_name}' has {ready}/{desired} replicas ready")
            return self.fail(
                "faulty_deployment_not_ready",
                deployment=deployment_name,
                namespace=namespace,
                ready=ready,
                desired=desired,
            )

        # Secondary check: the rest of the namespace is healthy (no agent-induced
        # collateral damage to other services).
        unready = self.pods_unready(kubectl.list_pods(namespace).items, namespace=namespace)
        if unready is not None:
            return unready

        print(f"✅ Deployment '{deployment_name}' has {ready}/{desired} replicas ready; all pods healthy")
        return {"success": True}
