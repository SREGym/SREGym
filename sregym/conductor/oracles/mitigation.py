import time

from sregym.conductor.oracles.base import Oracle

# Time to wait for deployments to settle after agent submission, so we
# evaluate a stable state rather than a transient rolling-update window.
_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class MitigationOracle(Oracle):
    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        # Populated by capture_baseline() once the app is deployed. It cannot be
        # filled in here: the Problem is built before deploy_app(), so the
        # namespace is still empty and every replica check below would be
        # skipped, letting "scale to 0" and "delete the deployment" pass.
        self.replica_count = {}

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
                status = dep.status
                desired = dep.spec.replicas if dep.spec.replicas is not None else 1
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
        results = {}

        # Wait for any in-progress rollouts to finish so we don't evaluate
        # a transient state where old pods are gone and new ones haven't crashed yet.
        self._wait_for_rollouts(kubectl, namespace)

        deployments = kubectl.list_deployments(namespace)
        current_deps = {dep.metadata.name: dep for dep in deployments.items}

        for name in self.replica_count:
            if name not in current_deps:
                print(f"❌ Deployment '{name}' was deleted")
                results["success"] = False
                return results
            dep = current_deps[name]
            desired = dep.spec.replicas if dep.spec.replicas is not None else 1
            if desired == 0:
                print(f"❌ Deployment '{name}' was scaled to 0")
                results["success"] = False
                return results
            ready = dep.status.ready_replicas or 0
            if ready < desired:
                print(f"❌ Deployment '{name}' has {ready}/{desired} replicas ready")
                results["success"] = False
                return results

        pod_list = kubectl.list_pods(namespace)

        if not pod_list.items:
            print("❌ No pods found in namespace")
            results["success"] = False
            return results

        all_normal = True

        for pod in pod_list.items:
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                all_normal = False
                break

            for container_status in pod.status.container_statuses:
                if container_status.state.waiting and container_status.state.waiting.reason:
                    print(f"❌ Container {container_status.name} is waiting: {container_status.state.waiting.reason}")
                    all_normal = False
                elif container_status.state.terminated and container_status.state.terminated.reason != "Completed":
                    print(
                        f"❌ Container {container_status.name} terminated: {container_status.state.terminated.reason}"
                    )
                    all_normal = False
                elif not container_status.ready:
                    print(f"⚠️ Container {container_status.name} is not ready")
                    all_normal = False

            if not all_normal:
                break

        results["success"] = all_normal
        return results
