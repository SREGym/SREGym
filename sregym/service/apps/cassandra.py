"""Cassandra application deployed via the K8ssandra operator (cass-operator)."""

import json
import logging
import subprocess
import time

from sregym.paths import BASE_DIR
from sregym.service.apps.base import Application
from sregym.service.helm import Helm
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.application")

CASSANDRA_METADATA = BASE_DIR / "service" / "metadata" / "cassandra.json"


def _run(cmd: str, check: bool = True, input: str | None = None) -> str:
    """Run a shell command, raise on failure, return stdout."""
    result = subprocess.run(
        cmd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        input=input,
    )
    if result.stdout:
        logger.debug(result.stdout.strip())
    if result.stderr:
        logger.debug(result.stderr.strip())
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd}\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout


class Cassandra(Application):
    def __init__(self, cassandra_version: str | None = None):
        super().__init__(CASSANDRA_METADATA)
        self.kubectl = KubeCtl()
        self.load_app_json()

        with open(CASSANDRA_METADATA) as f:
            metadata = json.load(f)

        k8s_cfg = metadata["K8ssandra Config"]
        self.app_name = metadata["Name"]
        self.description = metadata["Desc"]
        self.operator_namespace = k8s_cfg["operator_namespace"]
        self.operator_helm_repo = k8s_cfg["operator_helm_repo"]
        self.operator_helm_repo_url = k8s_cfg["operator_helm_repo_url"]
        self.operator_chart = k8s_cfg["operator_chart"]
        self.cluster_name = k8s_cfg["cluster_name"]
        self.datacenter_name = k8s_cfg["datacenter_name"]
        self.cluster_size = k8s_cfg["cluster_size"]
        self.cassandra_version = cassandra_version or k8s_cfg["cassandra_version"]

        # The K8ssandraCluster CR must live in the operator's namespace (RBAC is namespace-scoped).
        # Pods are created in the same namespace as the CR, so we override self.namespace here.
        self.namespace = self.operator_namespace

    def deploy(self):
        """Deploy Cassandra cluster via K8ssandra operator."""
        logger.info(f"Deploying Cassandra {self.cassandra_version} via K8ssandra operator")

        self._ensure_cert_manager()
        self._install_operator()
        self._deploy_cassandra_cluster()
        # Skip waiting - let the cluster come up asynchronously
        # self._wait_for_cluster_ready()

        logger.info("Cassandra cluster deployed (not waiting for ready)")

    def _ensure_cert_manager(self):
        """Install cert-manager if not already present (required by K8ssandra webhooks)."""
        result = subprocess.run(
            "kubectl get namespace cert-manager --ignore-not-found",
            shell=True,
            capture_output=True,
            text=True,
        )
        if "cert-manager" in result.stdout:
            logger.info("cert-manager already installed")
            return

        logger.info("Installing cert-manager (required by K8ssandra operator)...")
        _run("kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml")
        _run("kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=120s")
        # Wait for all cert-manager pods to be fully Ready (containers initialized).
        # The webhook TLS certificate is issued asynchronously after the controller
        # pod starts — pod Ready is a stronger signal than deployment Available.
        _run("kubectl wait pod --all -n cert-manager --for=condition=Ready --timeout=120s")
        logger.info("cert-manager ready")

    def _install_operator(self):
        """Install the K8ssandra operator via Helm (raises on failure)."""
        logger.info(f"Installing K8ssandra operator in namespace '{self.operator_namespace}'")

        try:
            Helm.add_repo(self.operator_helm_repo, self.operator_helm_repo_url)
            Helm.repo_update()
        except RuntimeError as e:
            logger.warning(f"Helm repo setup issue (continuing with cached charts): {e}")

        # Check if already installed
        existing = subprocess.run(
            f"helm status k8ssandra-operator -n {self.operator_namespace}",
            shell=True,
            capture_output=True,
            text=True,
        )
        if existing.returncode == 0:
            if "deployed" in existing.stdout:
                logger.info("K8ssandra operator already deployed, skipping install")
                return
            if "failed" in existing.stdout:
                logger.warning("K8ssandra operator release is in 'failed' state — uninstalling before reinstall")
                subprocess.run(
                    f"helm uninstall k8ssandra-operator -n {self.operator_namespace}",
                    shell=True,
                    check=False,
                )

        logger.info("Running helm upgrade --install for k8ssandra-operator...")
        # Retry to handle the cert-manager webhook TLS bootstrapping race: the
        # webhook pod may report Ready before its serving certificate is issued.
        helm_cmd = (
            f"helm upgrade --install k8ssandra-operator {self.operator_chart} "
            f"--namespace {self.operator_namespace} "
            f"--create-namespace "
            f"--set global.clusterScoped=true "
            f"--wait --timeout 5m"
        )
        for attempt in range(1, 4):
            try:
                _run(helm_cmd)
                break
            except RuntimeError as e:
                if attempt == 3 or "webhook" not in str(e):
                    raise
                logger.warning(
                    f"k8ssandra-operator install failed (attempt {attempt}/3) — "
                    f"cert-manager webhook not ready yet, retrying in 20s"
                )
                time.sleep(20)
        logger.info("K8ssandra operator installed and ready")

    def _deploy_cassandra_cluster(self):
        """Deploy the K8ssandraCluster CR using kubectl apply via stdin."""
        logger.info(f"Deploying Cassandra cluster '{self.cluster_name}' with {self.cluster_size} nodes")

        _run(f"kubectl create namespace {self.namespace} --dry-run=client -o yaml | kubectl apply -f -")

        manifest = self._build_cluster_manifest()
        _run("kubectl apply -f -", input=manifest)
        logger.info(f"K8ssandraCluster CR applied to namespace '{self.namespace}'")

    def _build_cluster_manifest(self) -> str:
        """Build the K8ssandraCluster custom resource YAML."""
        return f"""\
apiVersion: k8ssandra.io/v1alpha1
kind: K8ssandraCluster
metadata:
  name: {self.cluster_name}
  namespace: {self.namespace}
spec:
  cassandra:
    serverVersion: "{self.cassandra_version}"
    datacenters:
      - metadata:
          name: {self.datacenter_name}
        size: {self.cluster_size}
        storageConfig:
          cassandraDataVolumeClaimSpec:
            storageClassName: openebs-hostpath
            accessModes:
              - ReadWriteOnce
            resources:
              requests:
                storage: 5Gi
        resources:
          requests:
            memory: 1Gi
            cpu: 500m
          limits:
            memory: 2Gi
            cpu: "1"
        config:
          jvmOptions:
            heapSize: 512M
"""

    def _wait_for_cluster_ready(self):
        """Wait for the CassandraDatacenter Ready condition.

        K8ssandra starts Cassandra with -Dcassandra.skip_default_role_setup=true so
        there are no default users. The cass-operator creates the superuser as part
        of its reconciliation loop and only sets CassandraDatacenter Ready=True once
        that step completes. Pod readiness (2/2) fires several minutes earlier and is
        not a sufficient signal — waiting for it causes auth failures.
        """
        logger.info(f"Waiting for CassandraDatacenter '{self.datacenter_name}' Ready condition (up to 7 min)...")
        deadline = time.time() + 420

        while time.time() < deadline:
            result = subprocess.run(
                f"kubectl get cassandradatacenter {self.datacenter_name} -n {self.namespace} "
                f"-o jsonpath='{{range .status.conditions[*]}}{{.type}}={{.status}}\\n{{end}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            conditions = dict(line.split("=", 1) for line in result.stdout.strip().split("\n") if "=" in line)
            if conditions.get("Ready") == "True":
                logger.info("CassandraDatacenter Ready=True — superuser created, cluster fully ready")
                return
            logger.debug(f"Datacenter conditions: {conditions} — retrying in 15s")
            time.sleep(15)

        raise RuntimeError(f"Timeout waiting for CassandraDatacenter '{self.datacenter_name}' to be Ready")

    def _get_cql_credentials(self) -> tuple[str, str]:
        """Retrieve the superuser credentials from the K8ssandra-managed secret."""
        import base64

        secret_name = f"{self.cluster_name}-superuser"
        username = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.username}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        password = subprocess.run(
            f"kubectl get secret {secret_name} -n {self.namespace} -o jsonpath='{{.data.password}}'",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return base64.b64decode(username).decode(), base64.b64decode(password).decode()

    def run_cql(self, cql: str) -> str:
        """Execute CQL statement(s) against the cluster via cqlsh.

        Pipes CQL via stdin so multi-statement scripts work correctly.
        Credentials are base64-encoded before being embedded in the shell
        command so special characters (quotes, spaces, etc.) never break
        the shell quoting.
        """
        import base64 as _b64

        pod = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )

        if not pod:
            raise RuntimeError(f"No Cassandra pods found in namespace '{self.namespace}'")

        username, password = self._get_cql_credentials()
        u_b64 = _b64.b64encode(username.encode()).decode()
        p_b64 = _b64.b64encode(password.encode()).decode()

        result = subprocess.run(
            f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- "
            f"bash -c '"
            f"U=$(echo {u_b64} | base64 -d); "
            f"P=$(echo {p_b64} | base64 -d); "
            f'cqlsh -u "$U" -p "$P" --request-timeout=30'
            f"'",
            shell=True,
            capture_output=True,
            text=True,
            input=cql,
        )
        logger.info(f"CQL stdout: {result.stdout}")
        if result.stderr:
            logger.info(f"CQL stderr: {result.stderr}")
        if result.returncode != 0:
            raise RuntimeError(f"CQL execution failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return result.stdout

    def update_server_image(self, new_image: str, wait: bool = False):
        """Replace the running Cassandra cluster with a freshly built image.

        K8ssandra's rolling-update strategy stalls when the current pods are
        crash-looping (OOM, etc.) because it waits for each pod to become ready
        before updating the next.  To guarantee the new image is used we tear
        down the operator and cluster entirely, then redeploy from scratch.

        Sequence:
          1. Delete the K8ssandraCluster CR (removes StatefulSet + pods)
          2. Helm uninstall k8ssandra-operator
          3. Wait for resources to drain
          4. Helm install k8ssandra-operator
          5. Deploy a fresh K8ssandraCluster CR with ``serverImage: new_image``

        Args:
            new_image: Docker image tag to deploy.
            wait: If True, block until cluster is ready.
        """
        logger.info(f"Tearing down and redeploying cluster with image: {new_image}")

        # 1. Delete the cluster CR (operator will clean up StatefulSet, pods, etc.)
        logger.info("Deleting K8ssandraCluster CR...")
        _run(
            f"kubectl delete k8ssandracluster {self.cluster_name} -n {self.namespace} --ignore-not-found --timeout=60s",
            check=False,
        )

        # 2. Wait for Cassandra pods to drain
        logger.info("Waiting for Cassandra pods to terminate...")
        deadline = time.time() + 90
        while time.time() < deadline:
            result = subprocess.run(
                f"kubectl get pods -n {self.namespace} -l app.kubernetes.io/name=cassandra "
                f"--no-headers 2>/dev/null | wc -l",
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip() == "0":
                break
            time.sleep(5)

        # 3. Uninstall the operator
        logger.info("Uninstalling K8ssandra operator...")
        _run(
            f"helm uninstall k8ssandra-operator -n {self.operator_namespace} --wait",
            check=False,
        )
        # Wait for operator pods to drain
        deadline = time.time() + 60
        while time.time() < deadline:
            result = subprocess.run(
                f"kubectl get pods -n {self.operator_namespace} --no-headers 2>/dev/null | wc -l",
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip() == "0":
                break
            time.sleep(5)

        # 4. Update our image reference and redeploy everything
        if hasattr(self, "custom_image"):
            self.custom_image = new_image
        logger.info("Reinstalling K8ssandra operator...")
        self._install_operator()

        logger.info(f"Deploying fresh K8ssandraCluster with image: {new_image}")
        self._deploy_cassandra_cluster()

        if wait:
            self._wait_for_cluster_ready()
            logger.info(f"Cluster ready with new image: {new_image}")
        else:
            logger.info("Cluster deployed (not waiting for ready)")

    def delete(self):
        """Delete the Cassandra cluster CR."""
        subprocess.run(
            f"kubectl delete k8ssandracluster {self.cluster_name} -n {self.namespace} --ignore-not-found",
            shell=True,
            check=False,
        )

    def cleanup(self):
        """Full cleanup: cluster CR, namespaces, PVs, operator."""
        logger.info("Cleaning up Cassandra deployment")

        self.delete()

        self.kubectl.delete_namespace(self.namespace)

        # Clean up any leftover PVs bound to this namespace
        pvs_out = subprocess.run(
            f"kubectl get pv --no-headers | grep '{self.namespace}' || true",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        for line in pvs_out.strip().splitlines():
            if line:
                pv_name = line.split()[0]
                subprocess.run(
                    f'kubectl patch pv {pv_name} -p \'{{"metadata":{{"finalizers":null}}}}\'',
                    shell=True,
                    check=False,
                )
                subprocess.run(f"kubectl delete pv {pv_name} --ignore-not-found", shell=True, check=False)

        subprocess.run(
            f"helm uninstall k8ssandra-operator -n {self.operator_namespace} 2>/dev/null || true",
            shell=True,
            check=False,
        )
        self.kubectl.delete_namespace(self.operator_namespace)

        logger.info("Cassandra cleanup complete")

    def start_workload(self):
        """No-op — workload is driven by the problem's fault trigger."""
        pass

    def create_workload(self, **kwargs):
        """No-op."""
        pass


class CassandraWithCustomImage(Cassandra):
    """Cassandra deployment using a locally-built custom image.

    Pass ``custom_image`` as the Docker image name:tag produced by
    CassandraBuildManager.  The K8ssandraCluster manifest gains a
    ``serverImage`` field so the operator pulls this image instead of
    the default upstream one.
    """

    def __init__(self, cassandra_version: str, custom_image: str):
        super().__init__(cassandra_version=cassandra_version)
        self.custom_image = custom_image

    def _build_cluster_manifest(self) -> str:
        return f"""\
apiVersion: k8ssandra.io/v1alpha1
kind: K8ssandraCluster
metadata:
  name: {self.cluster_name}
  namespace: {self.namespace}
spec:
  cassandra:
    serverVersion: "{self.cassandra_version}"
    serverImage: "{self.custom_image}"
    datacenters:
      - metadata:
          name: {self.datacenter_name}
        size: {self.cluster_size}
        storageConfig:
          cassandraDataVolumeClaimSpec:
            storageClassName: openebs-hostpath
            accessModes:
              - ReadWriteOnce
            resources:
              requests:
                storage: 5Gi
        resources:
          requests:
            memory: 1Gi
            cpu: 500m
          limits:
            memory: 2Gi
            cpu: "1"
        config:
          jvmOptions:
            heapSize: 512M
"""
