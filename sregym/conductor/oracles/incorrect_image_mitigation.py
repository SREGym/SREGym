import contextlib
import json
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

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

    def __init__(self, problem, actual_images: dict | None = None):
        if actual_images is None:
            actual_images = {}
        super().__init__(problem)
        self.actual_images = actual_images

    def _deployments(self):
        return {
            name: self.problem.kubectl.get_deployment(name, self.problem.namespace)
            for name in self.problem.faulty_service
        }

    def _image_failure(self, deployments) -> dict | None:
        still_wrong = {}
        for name, deployment in deployments.items():
            container = deployment.spec.template.spec.containers[0]
            if normalize_image(container.image) == normalize_image(self.actual_images[name]):
                print(f"❌ Deployment {name} still using incorrect image: {container.image}")
                still_wrong[name] = container.image
        if still_wrong:
            return self.fail("fault_still_present", deployments=still_wrong)
        return None

    def _runtime_state(self, deployments) -> dict:
        """Keep pod identities: a replacement must not hide a reset restart count."""
        state = {}
        for name, deployment in deployments.items():
            pods = self.problem.kubectl.get_deployment_pods(deployment, self.problem.namespace)
            restarts = {
                (pod.metadata.uid, status.name): status.restart_count or 0
                for pod in pods
                if pod.metadata.deletion_timestamp is None
                for status in pod.status.container_statuses or []
            }
            state[name] = (deployment.metadata.uid, deployment.metadata.generation, restarts)
        return state

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Mitigation Evaluation ==")
        deployments = self._deployments()
        rollout_deadline = time.monotonic() + self.rollout_timeout_seconds
        stable_deadline = None
        initial_state = None
        while True:
            failure = self._image_failure(deployments)
            if failure is not None:
                return failure
            unready = [name for name, dep in deployments.items() if not deployment_rollout_complete(dep)]
            if unready:
                if stable_deadline is not None or time.monotonic() >= rollout_deadline:
                    print(f"❌ Deployments did not roll out and stay Ready: {', '.join(unready)}")
                    return self.fail("required_deployment_not_rolled_out", deployments=unready)
                deadline = rollout_deadline
            else:
                state = self._runtime_state(deployments)
                if any(not value[2] for value in state.values()):
                    return self.fail("no_matching_pods", deployments=list(deployments))
                if stable_deadline is None:
                    initial_state = state
                    stable_deadline = time.monotonic() + self.stability_seconds
                elif state != initial_state:
                    print("❌ A target rollout or container changed during the stability window")
                    return self.fail("required_deployment_not_rolled_out", deployments=list(deployments))
                if time.monotonic() >= stable_deadline:
                    print("✅ All target deployments stayed Ready without restarting")
                    return {"success": True}
                deadline = stable_deadline

            # All targets share both deadlines. Re-read every target each time,
            # including the final sample, rather than retaining earlier verdicts.
            time.sleep(min(self.poll_interval_seconds, max(0, deadline - time.monotonic())))
            deployments = self._deployments()


class ProductCatalogImageMitigationOracle(IncorrectImageMitigationOracle):
    """Astronomy Shop also needs a working catalog through its public frontend."""

    probe_timeout_seconds = 60
    request_timeout_seconds = 5
    probe_requests = 3
    expected_product_id = "OLJCESPC7Z"

    def _valid_products(self, output: str) -> bool:
        decoder = json.JSONDecoder()
        for _ in range(self.probe_requests):
            try:
                products, end = decoder.raw_decode(output.lstrip())
            except json.JSONDecodeError:
                return False
            if not isinstance(products, list) or not any(
                isinstance(product, dict)
                and product.get("id") == self.expected_product_id
                and isinstance(product.get("name"), str)
                and bool(product["name"])
                and isinstance(product.get("priceUsd"), dict)
                for product in products
            ):
                return False
            output = output.lstrip()[end:]
        return not output.strip()

    def _catalog_available(self) -> bool:
        namespace = self.problem.namespace
        api = self.problem.kubectl.core_v1_api
        service_name = self.problem.app.frontend_service
        service = api.read_namespaced_service(service_name, namespace)
        if not service.spec.ports:
            return False
        url = f"http://{service_name}.{namespace}.svc.cluster.local:{service.spec.ports[0].port}/api/products"
        name = f"catalog-connectivity-{time.time_ns()}"
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=name, namespace=namespace),
            spec=client.V1PodSpec(
                restart_policy="Never",
                active_deadline_seconds=self.probe_timeout_seconds,
                termination_grace_period_seconds=0,
                automount_service_account_token=False,
                containers=[
                    client.V1Container(
                        name="probe",
                        image="busybox:1.36",
                        image_pull_policy="IfNotPresent",
                        command=[
                            "sh",
                            "-ec",
                            f"for n in $(seq 1 {self.probe_requests}); do "
                            f"wget -q -T {self.request_timeout_seconds} -t 1 -O - '{url}'; printf '\\n'; done",
                        ],
                        resources=client.V1ResourceRequirements(
                            requests={"cpu": "5m", "memory": "16Mi"},
                            limits={"cpu": "100m", "memory": "32Mi"},
                        ),
                    )
                ],
            ),
        )
        try:
            api.create_namespaced_pod(namespace, pod)
            deadline = time.monotonic() + self.probe_timeout_seconds
            while True:
                phase = api.read_namespaced_pod(name, namespace).status.phase
                if phase in ("Succeeded", "Failed") or time.monotonic() >= deadline:
                    break
                time.sleep(self.poll_interval_seconds)
            if phase != "Succeeded":
                print(f"❌ Catalog request probe did not succeed (pod phase: {phase})")
                return False
            return self._valid_products(api.read_namespaced_pod_log(name, namespace))
        finally:
            with contextlib.suppress(ApiException):
                api.delete_namespaced_pod(name, namespace, grace_period_seconds=0)

    def evaluate(self, *args, **kwargs) -> dict:
        result = super().evaluate(*args, **kwargs)
        if not result["success"]:
            return result
        if not self._catalog_available():
            print("❌ The frontend did not return valid product data")
            return self.fail("connectivity_probe_failed", endpoint="/api/products")
        print("✅ The frontend returned valid products for every catalog request")
        return result
