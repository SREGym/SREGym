import base64
import json
import logging

from kubernetes import client

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


class ImagePullSecretMitigationOracle(Oracle):
    def evaluate(self) -> dict:
        print("== Mitigation Evaluation (Missing ImagePullSecret) ==")

        problem = self.problem
        namespace = problem.namespace
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        # Check 1: imagePullSecret must exist
        try:
            secret = v1.read_namespaced_secret(name=problem.secret_name, namespace=namespace)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info("imagePullSecret '%s' not found — FAIL", problem.secret_name)
                return {
                    "success": False,
                    "reason": f"imagePullSecret '{problem.secret_name}' does not exist.",
                }
            raise

        # Check 2: secret must be well-formed (valid dockerconfigjson with 'auths' key)
        data = secret.data or {}
        if ".dockerconfigjson" not in data:
            logger.info("imagePullSecret missing .dockerconfigjson key — FAIL")
            return {
                "success": False,
                "reason": "imagePullSecret is missing the .dockerconfigjson key.",
            }
        try:
            raw = base64.b64decode(data[".dockerconfigjson"])
            config = json.loads(raw)
            if "auths" not in config or not config["auths"]:
                raise ValueError("'auths' key missing or empty")
        except Exception as exc:
            logger.info("imagePullSecret .dockerconfigjson malformed: %s — FAIL", exc)
            return {
                "success": False,
                "reason": f"imagePullSecret .dockerconfigjson is malformed: {exc}",
            }

        # Check 3: Deployment must reference the imagePullSecret
        deploy = apps_v1.read_namespaced_deployment(name=problem.target_deployment, namespace=namespace)
        pull_secrets = deploy.spec.template.spec.image_pull_secrets or []
        referenced = [s.name for s in pull_secrets]
        if problem.secret_name not in referenced:
            logger.info(
                "Deployment '%s' does not reference secret '%s' — FAIL",
                problem.target_deployment,
                problem.secret_name,
            )
            return {
                "success": False,
                "reason": (
                    f"Deployment '{problem.target_deployment}' does not reference "
                    f"the imagePullSecret '{problem.secret_name}' in its pod spec."
                ),
            }

        # Check 4: target pod must be Running and Ready
        pods = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"io.kompose.service={problem.target_deployment}",
        )
        for pod in pods.items:
            phase = (pod.status or client.V1PodStatus()).phase
            if phase != "Running":
                logger.info(
                    "Pod '%s' is in phase '%s', not Running — FAIL",
                    pod.metadata.name,
                    phase,
                )
                return {
                    "success": False,
                    "reason": f"Pod '{pod.metadata.name}' is in phase '{phase}', not Running.",
                }
            for cs in pod.status.container_statuses or []:
                if not cs.ready:
                    logger.info(
                        "Container '%s' in pod '%s' is not ready — FAIL",
                        cs.name,
                        pod.metadata.name,
                    )
                    return {
                        "success": False,
                        "reason": (f"Container '{cs.name}' in pod '{pod.metadata.name}' is not ready."),
                    }

        logger.info("All imagePullSecret mitigation checks passed ✅")
        return {"success": True}
