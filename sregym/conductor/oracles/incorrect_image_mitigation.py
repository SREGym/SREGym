import time

from sregym.conductor.oracles.base import Oracle
from sregym.service.rollout import deployment_rollout_complete

_DOCKER_HUB_PREFIXES = ("docker.io/library/", "index.docker.io/library/", "docker.io/", "index.docker.io/")


def normalize_image(reference: str) -> str:
    """Spell an image reference the way the registry resolves it.

    ``app-image:latest`` and ``docker.io/library/app-image:latest`` pull the
    same image, so comparing the strings is not enough. A digest names the
    content, so when one is present the tag is dropped.
    """
    name, _, digest = reference.strip().partition("@")
    for prefix in _DOCKER_HUB_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    repository, tag = name, "latest"
    if ":" in name.rsplit("/", 1)[-1]:
        repository, tag = name.rsplit(":", 1)
    return f"{repository}@{digest}" if digest else f"{repository}:{tag}"


class IncorrectImageMitigationOracle(Oracle):
    importance = 1.0
    rollout_timeout_seconds = 120
    poll_interval_seconds = 2
    # A container that crash-loops is Ready between restarts, and these apps
    # declare no readiness probes, so one Ready sample proves little.
    stability_seconds = 30

    def __init__(self, problem, actual_images: dict = None):
        if actual_images is None:
            actual_images = {}
        super().__init__(problem)
        self.actual_images = actual_images

    def _wait_for_current_rollout(self, deployment):
        deadline = time.monotonic() + self.rollout_timeout_seconds
        while True:
            if deployment_rollout_complete(deployment):
                return deployment
            if time.monotonic() >= deadline:
                return None

            time.sleep(self.poll_interval_seconds)
            deployment = self.problem.kubectl.get_deployment(deployment.metadata.name, self.problem.namespace)

    def _restarts(self, deployment) -> int:
        pods = self.problem.kubectl.get_deployment_pods(deployment, self.problem.namespace)
        return sum(status.restart_count or 0 for pod in pods for status in pod.status.container_statuses or [])

    def _stays_ready(self, deployment) -> bool:
        """The rollout must hold, with no container restart, for stability_seconds."""
        restarts = self._restarts(deployment)
        deadline = time.monotonic() + self.stability_seconds
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval_seconds)
            deployment = self.problem.kubectl.get_deployment(deployment.metadata.name, self.problem.namespace)
            if not deployment_rollout_complete(deployment) or self._restarts(deployment) > restarts:
                return False
        return True

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        deployment_names = self.problem.faulty_service

        # The loop checks every faulty Deployment rather than stopping at the
        # first, so collect them all: which ones were left broken is more
        # useful than knowing that at least one was.
        still_wrong = {}
        replaced = []
        for deployment_name in deployment_names:
            # Fetch the current deployment
            deployment = kubectl.get_deployment(deployment_name, namespace)
            container = deployment.spec.template.spec.containers[0]
            actual_image = container.image

            if normalize_image(actual_image) == normalize_image(self.actual_images[deployment_name]):
                print(f"❌ Deployment {deployment_name} still using incorrect image: {actual_image}")
                still_wrong[deployment_name] = actual_image
            else:
                print(f"✅ Deployment {deployment_name} using correct image: {actual_image}")
                replaced.append(deployment)

        if still_wrong:
            # Compared against the exact image the injector wrote, so this is
            # the injected fault observed directly.
            return self.fail("fault_still_present", deployments=still_wrong)

        # Another image string is not a fix until it pulls, starts and stays
        # Ready. A rollout also needs at least one replica, so a Deployment
        # left at zero does not pass.
        for deployment in replaced:
            name = deployment.metadata.name
            current = self._wait_for_current_rollout(deployment)
            if current is None or not self._stays_ready(current):
                print(f"❌ Deployment {name} did not roll out its new image and stay Ready")
                return self.fail("required_deployment_not_rolled_out", deployment=name)
        return {"success": True}
