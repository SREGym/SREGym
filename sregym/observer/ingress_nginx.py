import json
import logging
import subprocess
import time

from sregym.service.rollout import deployment_rollout_complete

logger = logging.getLogger("all.sregym.ingress_nginx")


class IngressNginx:
    def __init__(self):
        self.namespace = "ingress-nginx"
        self.release_name = "ingress-nginx"

    def run_cmd(self, cmd: str) -> str:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Command failed: {cmd}\nError: {result.stderr}")
        return result.stdout.strip()

    def deploy(self):
        """Deploy the nginx ingress controller via Helm."""
        # Ensure the helm repo is available
        self.run_cmd("helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true")
        self.run_cmd("helm repo update ingress-nginx")

        self._remove_orphaned_cluster_objects()
        # Install or upgrade the chart
        self.run_cmd(
            f"helm upgrade --install {self.release_name} ingress-nginx/ingress-nginx "
            f"--namespace {self.namespace} --create-namespace "
            "--set controller.service.type=ClusterIP "
            "--set controller.ingressClassResource.default=true "
            "--set controller.watchIngressWithoutClass=true"
        )
        self._wait_for_ready(timeout=120)
        logger.info("Nginx ingress controller deployed successfully.")

    def _remove_orphaned_cluster_objects(self) -> None:
        """Delete the chart's cluster-scoped objects left behind by a release that no longer exists.

        Deleting the ingress-nginx namespace (as cleanup between attempts does) removes the
        release record but not its IngressClass, ClusterRoles or webhook configuration, and
        Helm then refuses to adopt them (``metadata.managedFields must be nil``).
        """
        if subprocess.run(
            f"helm status {self.release_name} -n {self.namespace}", shell=True, capture_output=True
        ).returncode == 0:
            return
        self.run_cmd(
            "kubectl delete ingressclass,clusterrole,clusterrolebinding,validatingwebhookconfiguration "
            f"-l app.kubernetes.io/instance={self.release_name} --ignore-not-found"
        )

    def _wait_for_ready(self, timeout: int = 120):
        """Wait until the ingress controller deployment is ready."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                out = self.run_cmd(f"kubectl -n {self.namespace} get deployment ingress-nginx-controller -o json")
                if deployment_rollout_complete(json.loads(out)):
                    return
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError(f"Nginx ingress controller not ready within {timeout}s")
