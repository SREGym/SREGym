import time

from sregym.conductor.oracles.base import Oracle

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5
_INVALID_TAG = "nonexistent-tag-xyzzy"


class ImagePullBackoffMitigationOracle(Oracle):
    """Stricter mitigation oracle for ImagePullBackoff problems.

    Checks:
    1. The faulty deployment still exists (not deleted as a shortcut).
    2. The deployment has at least 1 desired replica (not scaled to 0).
    3. The container image no longer uses the injected invalid tag.
    4. All pods in the namespace are Running and ready.
    """

    importance = 1.0

    def __init__(self, problem, faulty_service: str, invalid_tag: str = _INVALID_TAG):
        super().__init__(problem)
        self.faulty_service = faulty_service
        self.invalid_tag = invalid_tag

    def _wait_for_rollouts(self, kubectl, namespace):
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = all(
                (dep.status.updated_replicas or 0) >= (dep.spec.replicas or 1)
                and (dep.status.ready_replicas or 0) >= (dep.spec.replicas or 1)
                and (dep.status.unavailable_replicas or 0) == 0
                for dep in deployments.items
            )
            if all_settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print("⚠️ Timed out waiting for rollouts to settle; evaluating current state")

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (ImagePullBackoff) ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace

        # 1. Deployment must still exist
        try:
            deployment = kubectl.get_deployment(self.faulty_service, namespace)
        except Exception:
            print(f"❌ Deployment '{self.faulty_service}' not found — deleting it is not a valid fix")
            return {"success": False}

        # 2. Deployment must have >= 1 desired replica
        desired = deployment.spec.replicas or 0
        if desired < 1:
            print(
                f"❌ Deployment '{self.faulty_service}' scaled to {desired} replicas — scaling to 0 is not a valid fix"
            )
            return {"success": False}

        # 3. Container image must not still use the invalid tag
        containers = deployment.spec.template.spec.containers
        for container in containers:
            if self.invalid_tag in (container.image or ""):
                print(f"❌ Container '{container.name}' still uses invalid image: {container.image}")
                return {"success": False}
        print(f"✅ Deployment '{self.faulty_service}' image tag is fixed")

        # 4. All pods must be Running and ready
        self._wait_for_rollouts(kubectl, namespace)
        pod_list = kubectl.list_pods(namespace)

        if not pod_list.items:
            print("❌ No pods found in namespace")
            return {"success": False}

        for pod in pod_list.items:
            if pod.status.phase != "Running":
                print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                return {"success": False}
            for cs in pod.status.container_statuses or []:
                if cs.state.waiting and cs.state.waiting.reason:
                    print(f"❌ Container {cs.name} is waiting: {cs.state.waiting.reason}")
                    return {"success": False}
                if not cs.ready:
                    print(f"❌ Container {cs.name} is not ready")
                    return {"success": False}

        print("✅ All pods are Running and ready")
        return {"success": True}
