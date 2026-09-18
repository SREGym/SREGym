import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.rollout import deployment_rollout_complete

# Time to wait for deployments to settle after agent submission, so we
# evaluate a stable state rather than a transient rolling-update window.
_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class MitigationOracle(Oracle):
    importance = 1.0

    # The default mitigation oracle, referenced by 68 problem files -- the
    # widest-reaching classification in the codebase.
    #
    # It is also one of the few oracles that can attribute a deletion, because
    # ``capture_baseline`` records the Deployment names and replica counts while
    # the app is healthy and *before* the fault is injected. A Deployment that
    # was in that snapshot and is now gone did not vanish on its own, and the
    # fault injection does not delete Deployments -- so unlike the shared
    # AMBIGUOUS default, here it is the agent's doing. The class docstring on
    # ``capture_baseline`` already notes this is exactly what stops "scale to 0"
    # and "delete the deployment" passing.
    FAILURE_CLASSES = {
        "required_deployment_missing": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        # Populated by capture_baseline() once the app is deployed. It cannot be
        # filled in here: the Problem is built before deploy_app(), so the
        # namespace is still empty and every replica check below would be
        # skipped, letting "scale to 0" and "delete the deployment" pass.
        self.replica_count = {}
        self.rollout_time = _ROLLOUT_SETTLE_SECONDS

    def capture_baseline(self) -> None:
        """Capture pre-injection Deployments in the problem namespace.

        This is not a full resource baseline: Services and resources created by
        inject_fault() are outside it. Faults that must preserve or validate
        those resources need a custom mitigation oracle.
        """
        deployments = self.problem.kubectl.list_deployments(self.problem.namespace)
        self.replica_count = {dep.metadata.name: dep.spec.replicas for dep in deployments.items}
        self.rollout_time = _ROLLOUT_SETTLE_SECONDS

    def _wait_for_rollouts(self, kubectl, namespace):
        """Wait for all deployments in the namespace to finish rolling out."""
        deadline = time.monotonic() + self.rollout_time
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = True
            for dep in deployments.items:
                if not deployment_rollout_complete(dep, allow_zero=True):
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

        # Wait for any in-progress rollouts to finish so we don't evaluate
        # a transient state where old pods are gone and new ones haven't crashed yet.
        self._wait_for_rollouts(kubectl, namespace)

        deployments = kubectl.list_deployments(namespace)
        current_deps = {dep.metadata.name: dep for dep in deployments.items}

        for name in self.replica_count:
            if name not in current_deps:
                print(f"❌ Deployment '{name}' was deleted")
                return self.fail("required_deployment_missing", deployment=name, namespace=namespace)
        # Recheck after settling: a timeout must not turn an incomplete rollout
        # into a pass. Include Deployments added after the baseline as well.
        for name, dep in current_deps.items():
            desired = dep.spec.replicas if dep.spec.replicas is not None else 1
            if desired == 0 and name in self.replica_count:
                print(f"❌ Deployment '{name}' was scaled to 0")
                return self.fail("required_deployment_scaled_to_zero", deployment=name, namespace=namespace)
            ready = getattr(dep.status, "ready_replicas", None) or 0
            if not deployment_rollout_complete(dep, allow_zero=name not in self.replica_count):
                print(f"❌ Deployment '{name}' rollout is incomplete ({ready}/{desired} replicas ready)")
                return self.fail(
                    "deployment_replicas_unready",
                    deployment=name,
                    namespace=namespace,
                    ready=ready,
                    desired=desired,
                )

        pod_list = kubectl.list_pods(namespace)

        if not pod_list.items:
            print("❌ No pods found in namespace")
            return self.fail("no_pods_found", namespace=namespace)

        # The original loop tracked an ``all_normal`` flag and broke out of two
        # levels, which left nowhere to record *which* pod was at fault.
        unready = self.pods_unready(pod_list.items, namespace=namespace)
        if unready is not None:
            return unready

        return {"success": True}
