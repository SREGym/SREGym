import time

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.service.rollout import deployment_rollout_complete

# Time to wait for deployments to settle after agent submission, so we
# evaluate a stable state rather than a transient rolling-update window.
_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5

# Namespaced objects besides Deployments that the app depends on. Deleting one
# leaves every pod Ready (until the next restart), so a pod walk cannot see it.
_BASELINE_RESOURCES = (
    ("Service", "core_v1_api", "list_namespaced_service"),
    ("PersistentVolumeClaim", "core_v1_api", "list_namespaced_persistent_volume_claim"),
    ("ConfigMap", "core_v1_api", "list_namespaced_config_map"),
    ("StatefulSet", "apps_v1_api", "list_namespaced_stateful_set"),
)


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
        # Same reasoning for the other baseline objects: the agent either
        # deleted one or did not restore the one the fault removed.
        "required_resource_missing": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        # Populated by capture_baseline() once the app is deployed. It cannot be
        # filled in here: the Problem is built before deploy_app(), so the
        # namespace is still empty and every replica check below would be
        # skipped, letting "scale to 0" and "delete the deployment" pass.
        self.replica_count = {}
        self.resources = {}
        self.rollout_time = _ROLLOUT_SETTLE_SECONDS

    def capture_baseline(self) -> None:
        """Capture pre-injection Deployments and the objects they depend on.

        Records Deployment replica counts and the names of the namespace's
        Services, PersistentVolumeClaims, ConfigMaps and StatefulSets. Objects
        created by inject_fault() are outside it. Faults that must validate
        those resources need a custom mitigation oracle.
        """
        deployments = self.problem.kubectl.list_deployments(self.problem.namespace)
        self.replica_count = {dep.metadata.name: dep.spec.replicas for dep in deployments.items}
        self.resources = {
            kind: set(items) for kind, items in self._list_resources(self.problem.kubectl, self.problem.namespace)
        }
        self.rollout_time = _ROLLOUT_SETTLE_SECONDS

    @staticmethod
    def _list_resources(kubectl, namespace):
        """Yield (kind, {name: object}) for each kind in _BASELINE_RESOURCES."""
        for kind, api, method in _BASELINE_RESOURCES:
            items = getattr(getattr(kubectl, api), method)(namespace).items
            yield kind, {item.metadata.name: item for item in items}

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
                self._wait_for_terminating_pods(kubectl, namespace)
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print("⚠️ Timed out waiting for deployments to settle; evaluating current state")

    def _wait_for_terminating_pods(self, kubectl, namespace, timeout: float = 120.0):
        """Let pods replaced by a completed rollout finish terminating.

        A rollout reports complete while the old ReplicaSet's pods are still
        Terminating; graded at that instant they read as unready (or Pending, for
        a pod stuck in Init). Pods still terminating after the timeout are graded.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pods = list(kubectl.list_pods(namespace).items)
                terminating = any(getattr(pod.metadata, "deletion_timestamp", None) for pod in pods)
            except Exception:  # pod state unreadable here; grade as before
                return
            if not terminating:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        # Wait for any in-progress rollouts to finish so we don't evaluate
        # a transient state where old pods are gone and new ones haven't crashed yet.
        self._wait_for_rollouts(kubectl, namespace)
        return self._evaluate_current_state()

    def _evaluate_current_state(self) -> dict:
        """Check current application health without another rollout grace period."""
        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        failure = self._deployment_failure(kubectl.list_deployments(namespace))
        if failure is not None:
            return failure

        pod_list = kubectl.list_pods(namespace)

        if not pod_list.items:
            print("❌ No pods found in namespace")
            return self.fail("no_pods_found", namespace=namespace)

        unready = self.pods_unready(pod_list.items, namespace=namespace)
        if unready is not None:
            return unready

        if self.resources:
            current = dict(self._list_resources(kubectl, namespace))
            for kind, names in self.resources.items():
                for name in sorted(names):
                    item = current[kind].get(name)
                    # A PVC still mounted by a pod stays, Terminating, until the
                    # pod goes; that is already a deletion.
                    if item is None or item.metadata.deletion_timestamp is not None:
                        print(f"❌ {kind} '{name}' from the healthy baseline is missing")
                        return self.fail("required_resource_missing", kind=kind, name=name, namespace=namespace)

        return {"success": True}

    def _deployment_failure(self, deployments) -> dict | None:
        """Check current rollouts against the pre-injection required workloads."""
        namespace = self.problem.namespace
        current_deps = {dep.metadata.name: dep for dep in deployments.items}

        for name in self.replica_count:
            if name not in current_deps:
                print(f"❌ Deployment '{name}' was deleted")
                return self.fail("required_deployment_missing", deployment=name, namespace=namespace)
        # Recheck after settling: a timeout must not turn an incomplete rollout
        # into a pass. Include Deployments added after the baseline as well.
        for name, dep in current_deps.items():
            desired = dep.spec.replicas if dep.spec.replicas is not None else 1
            # A component the app ships at 0 replicas (Frappe's optional workers) was 0 at baseline too.
            baseline_zero = self.replica_count.get(name) == 0
            if desired == 0 and name in self.replica_count and not baseline_zero:
                print(f"❌ Deployment '{name}' was scaled to 0")
                return self.fail("required_deployment_scaled_to_zero", deployment=name, namespace=namespace)
            ready = getattr(dep.status, "ready_replicas", None) or 0
            if not deployment_rollout_complete(dep, allow_zero=name not in self.replica_count or baseline_zero):
                print(f"❌ Deployment '{name}' rollout is incomplete ({ready}/{desired} replicas ready)")
                return self.fail(
                    "deployment_replicas_unready",
                    deployment=name,
                    namespace=namespace,
                    ready=ready,
                    desired=desired,
                )

        return None
