import logging
import re

from kubernetes import client

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


# Can be overriden by problem.target_pod_label to ensure it fits both blueprint_hotel_reservation
# and astronomy_shop versions of the problems.
_DEFAULT_POD_LABEL = "io.kompose.service"
_DEFAULT_GATED_IMAGE_RE = re.compile(r":5000/.*hotel-reservation")
_IMAGE_PULL_FAILURE_REASONS = {"ImagePullBackOff", "ErrImagePull", "ImageInspectError", "InvalidImageName"}


class ImagePullSecretMitigationOracle(Oracle):
    """
    Outcome-based oracle: the fault is considered mitigated when the target
    workload is actually Running + Ready (no image-pull failure), *regardless* of the
    credential-delivery mechanism — a secret named anything, or the ServiceAccount's
    imagePullSecrets all count. An anti-cheat guard still requires the running pod to
    pull the gated private-registry image, so the fault can't be "fixed" by repointing
    the Deployment to a public image instead of supplying valid credentials.
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (Missing ImagePullSecret) ==")

        problem = self.problem
        namespace = problem.namespace
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()
        target_deployment = problem.target_deployment
        target_container = problem.target_container
        # Per-problem overrides; default to the original hotel-reservation values so
        # the legacy problem keeps working unchanged.
        pod_label = getattr(problem, "target_pod_label", None) or _DEFAULT_POD_LABEL
        gated_image_re = getattr(problem, "gated_image_re", None) or _DEFAULT_GATED_IMAGE_RE

        # Primary criterion: the target workload's pods must be Running + Ready with
        # no image-pull failure. Accepts ANY working credential mechanism.
        pods = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"{pod_label}={target_deployment}",
        )
        if not pods.items:
            logger.info("No pods found for deployment '%s' — FAIL", target_deployment)
            return {"success": False, "reason": f"No pods found for deployment '{target_deployment}'."}

        for pod in pods.items:
            status = pod.status or client.V1PodStatus()

            # Surface an image-pull failure explicitly on any container, whatever the phase.
            for cs in status.container_statuses or []:
                waiting = cs.state.waiting if (cs.state and cs.state.waiting) else None
                if waiting and waiting.reason in _IMAGE_PULL_FAILURE_REASONS:
                    logger.info(
                        "Container '%s' in pod '%s' is failing to pull its image (%s) — FAIL",
                        cs.name,
                        pod.metadata.name,
                        waiting.reason,
                    )
                    return {
                        "success": False,
                        "reason": (
                            f"Container '{cs.name}' in pod '{pod.metadata.name}' is failing to "
                            f"pull its image ({waiting.reason})."
                        ),
                    }

            if status.phase != "Running":
                logger.info("Pod '%s' is in phase '%s', not Running — FAIL", pod.metadata.name, status.phase)
                return {
                    "success": False,
                    "reason": f"Pod '{pod.metadata.name}' is in phase '{status.phase}', not Running.",
                }
            for cs in status.container_statuses or []:
                if not cs.ready:
                    logger.info("Container '%s' in pod '%s' is not ready — FAIL", cs.name, pod.metadata.name)
                    return {
                        "success": False,
                        "reason": f"Container '{cs.name}' in pod '{pod.metadata.name}' is not ready.",
                    }

        # Anti-cheat guard: the Deployment must still point the target container at the
        # gated private-registry image, so the fix supplies credentials rather than
        # repointing to a public image.
        deploy = apps_v1.read_namespaced_deployment(name=target_deployment, namespace=namespace)
        containers = deploy.spec.template.spec.containers or []
        target = next((c for c in containers if c.name == target_container), containers[0] if containers else None)
        image = (target.image if target else "") or ""
        if not gated_image_re.search(image):
            logger.info("Anti-cheat: target image '%s' is not the gated private-registry image — FAIL", image)
            return {
                "success": False,
                "reason": (
                    f"Deployment '{target_deployment}' container image '{image}' is not the gated "
                    f"private-registry image. The fault must be fixed by supplying valid registry "
                    f"credentials, not by repointing the Deployment to a public image."
                ),
            }

        logger.info("All imagePullSecret mitigation checks passed ✅")
        return {"success": True}
