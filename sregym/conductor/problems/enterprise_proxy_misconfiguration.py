import logging

from kubernetes import client

from sregym.conductor.problems.base import ProblemBase
from sregym.service.kubectl import Kubectl

logger = logging.getLogger(__name__)


class EnterpriseProxyMisconfiguration(ProblemBase):
    """
    Simulates a scenario where an enterprise proxy is globally injected via a
    Mutating Admission Webhook, intercepting internal service mesh traffic.
    The agent must diagnose the 502 Bad Gateway errors, bypass the clean
    deployment manifests, inspect the live pod environment, and explicitly
    add NO_PROXY configurations.
    """

    def __init__(self):
        super().__init__()
        self.k8s_apps_v1 = client.AppsV1Api()
        self.k8s_core_v1 = client.CoreV1Api()
        self.kubectl = Kubectl()
        self.target_namespace = "default"
        self.target_deployment = "frontend"

    def inject(self) -> bool:
        """Deploys the webhook and triggers the fault."""
        logger.info("Injecting Enterprise Proxy Misconfiguration via Mutating Webhook...")

        # 1. Deploy the Webhook Server (SREGym handles the Docker build/load step via config)
        # We apply the webhook deployment and service to the cluster
        webhook_yaml_path = "proxy_webhook/webhook-deployment.yaml"
        self.kubectl.apply(webhook_yaml_path)

        # 2. Wait for the webhook server to become ready
        self.kubectl.wait_for_deployment("proxy-webhook-server", namespace="webhook-system")

        # 3. Apply the MutatingWebhookConfiguration to intercept pod creation
        config_yaml_path = "proxy_webhook/mutating-webhook-configuration.yaml"
        self.kubectl.apply(config_yaml_path)

        # 4. Restart the target deployment to trigger the webhook injection
        logger.info(f"Restarting {self.target_deployment} pods to trigger proxy injection...")
        self.kubectl.rollout_restart("deployment", self.target_deployment, self.target_namespace)

        return True

    def evaluate(self) -> bool:
        """
        Evaluates if the AI agent successfully mitigated the fault.
        The agent succeeds if it adds NO_PROXY=.cluster.local to the deployment.
        """
        try:
            # Fetch the current state of the deployment
            deployment = self.k8s_apps_v1.read_namespaced_deployment(
                name=self.target_deployment, namespace=self.target_namespace
            )

            # Check all containers in the deployment for the NO_PROXY variable
            containers = deployment.spec.template.spec.containers
            for container in containers:
                if container.env:
                    for env_var in container.env:
                        # The exact string the agent needs to add to bypass the trap
                        if env_var.name == "NO_PROXY" and ".cluster.local" in env_var.value:
                            logger.info("Mitigation successful: NO_PROXY detected.")
                            return True

            logger.info("Mitigation failed: NO_PROXY not found or incorrectly configured.")
            return False

        except Exception as e:
            logger.error(f"Error evaluating mitigation: {e}")
            return False

    def cleanup(self) -> bool:
        """Removes the webhook and restores the cluster to a healthy state."""
        logger.info("Cleaning up Enterprise Proxy Misconfiguration...")
        self.kubectl.delete_file("proxy_webhook/mutating-webhook-configuration.yaml")
        self.kubectl.delete_file("proxy_webhook/webhook-deployment.yaml")
        self.kubectl.rollout_restart("deployment", self.target_deployment, self.target_namespace)
        return True
