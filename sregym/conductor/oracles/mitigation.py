import time

from sregym.conductor.oracles.base import Oracle

# Time to wait for deployments to settle after agent submission, so we
# evaluate a stable state rather than a transient rolling-update window.
_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5


class MitigationOracle(Oracle):
    importance = 1.0

    def _wait_for_rollouts(self, kubectl, namespace):
        """Wait for all deployments in the namespace to finish rolling out."""
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
        results = {}

        # Wait for any in-progress rollouts to finish so we don't evaluate
        # a transient state where old pods are gone and new ones haven't crashed yet.
        self._wait_for_rollouts(kubectl, namespace)

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
