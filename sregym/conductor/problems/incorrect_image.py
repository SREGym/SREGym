from dataclasses import dataclass

from sregym.conductor.oracles.incorrect_image_mitigation import IncorrectImageMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


@dataclass(frozen=True)
class _OriginalImage:
    deployment_uid: str
    container_name: str
    image: str


class IncorrectImage(Problem):
    def __init__(self):
        super().__init__(app=AstronomyShop())
        self.kubectl = KubeCtl()
        self.faulty_service = ["product-catalog"]
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        self._original_images: dict[str, _OriginalImage] = {}
        self.root_cause = self.build_structured_root_cause(
            component="deployment/product-catalog",
            namespace=self.namespace,
            description=(
                "The product-catalog deployment is configured to pull a non-existent image tag (app-image:latest), "
                "so pods fail with image pull errors and the catalog path becomes unavailable. "
                "Symptoms typically include ImagePullBackOff events and upstream checkout calls timing out or failing."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = IncorrectImageMitigationOracle(
            problem=self, actual_images={"product-catalog": "app-image:latest"}
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        for service in self.faulty_service:
            deployment = self.kubectl.get_deployment(service, self.namespace)
            if service not in self._original_images:
                container = deployment.spec.template.spec.containers[0]
                self._original_images[service] = _OriginalImage(
                    deployment.metadata.uid, container.name, container.image
                )
            elif deployment.metadata.uid != self._original_images[service].deployment_uid:
                raise RuntimeError(f"Deployment '{service}' was recreated after its original image was saved")
            # Capture before mutation, including when injection fails partway
            # through. Repeated injection must retain the first clean image.
            self.injector.inject_incorrect_image(
                deployment_name=service, namespace=self.namespace, bad_image="app-image:latest"
            )
            print(f"Service: {service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for service in self.faulty_service:
            original = self._original_images.get(service)
            if original is None:
                raise RuntimeError(f"Original image for '{service}' is missing; refusing to guess a recovery tag")
            deployment = self.kubectl.get_deployment(service, self.namespace)
            if deployment.metadata.uid != original.deployment_uid:
                raise RuntimeError(f"Deployment '{service}' was recreated; refusing to restore a stale image")
            if original.container_name not in {c.name for c in deployment.spec.template.spec.containers}:
                raise RuntimeError(f"Original container '{original.container_name}' is missing from '{service}'")
            # Patch by saved container name, not list position. Include the UID
            # so Kubernetes also rejects a replacement racing the read above.
            self.kubectl.patch_deployment(
                name=service,
                namespace=self.namespace,
                patch_body={
                    "metadata": {"uid": original.deployment_uid},
                    "spec": {
                        "template": {
                            "spec": {"containers": [{"name": original.container_name, "image": original.image}]}
                        }
                    },
                },
            )
