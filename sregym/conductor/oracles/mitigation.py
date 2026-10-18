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
        Services, PersistentVolumeClaims and StatefulSets. ConfigMaps follow
        current workload references instead, so safe replacements can pass.
        Other objects created by inject_fault() are outside the baseline.
        Faults that must validate those resources need a custom oracle.
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

        deployments = kubectl.list_deployments(namespace)
        failure = self._deployment_failure(deployments)
        if failure is not None:
            return failure

        pod_list = kubectl.list_pods(namespace)

        if not pod_list.items:
            print("❌ No pods found in namespace")
            return self.fail("no_pods_found", namespace=namespace)

        unready = self.pods_unready(pod_list.items, namespace=namespace)
        if unready is not None:
            return unready

        current = dict(self._list_resources(kubectl, namespace)) if self.resources else {}
        required = dict(self.resources)
        pod_specs = [getattr(pod, "spec", None) for pod in pod_list.items if not self.is_completed_job_pod(pod)]
        # Templates cover the next pod too, including StatefulSets with OnDelete updates.
        for controller in [*deployments.items, *current.get("StatefulSet", {}).values()]:
            spec = getattr(controller, "spec", None)
            if getattr(spec, "replicas", None) == 0:
                continue
            template = getattr(spec, "template", None)
            pod_specs.append(getattr(template, "spec", None))
        configmaps = self._required_configmaps(pod_specs)
        if configmaps:
            required["ConfigMap"] = configmaps
            current["ConfigMap"] = {
                item.metadata.name: item for item in kubectl.core_v1_api.list_namespaced_config_map(namespace).items
            }

        for kind, names in required.items():
            for name in sorted(names):
                item = current[kind].get(name)
                # A mounted PVC can remain Terminating until its pod exits.
                if item is None or item.metadata.deletion_timestamp is not None:
                    print(f"❌ Required {kind} '{name}' is missing or Terminating")
                    return self.fail("required_resource_missing", kind=kind, name=name, namespace=namespace)

        return {"success": True}

    @staticmethod
    def _required_configmaps(pod_specs) -> set[str]:
        """Collect non-optional native references, not historical object names."""
        references = []
        for spec in pod_specs:
            for volume in getattr(spec, "volumes", None) or []:
                references.append(getattr(volume, "config_map", None))
                projected = getattr(volume, "projected", None)
                references.extend(
                    getattr(source, "config_map", None) for source in getattr(projected, "sources", None) or []
                )
            containers = [
                *(getattr(spec, "containers", None) or []),
                *(getattr(spec, "init_containers", None) or []),
            ]
            for container in containers:
                references.extend(
                    getattr(source, "config_map_ref", None) for source in getattr(container, "env_from", None) or []
                )
                for variable in getattr(container, "env", None) or []:
                    references.append(getattr(variable.value_from, "config_map_key_ref", None))
        return {reference.name for reference in references if reference is not None and not reference.optional}

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
