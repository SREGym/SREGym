import base64
import contextlib
import json
import logging
import os
import shlex
import socket
import subprocess
import tempfile
import time

from kubernetes import client

from sregym.conductor.oracles.image_pull_secret_mitigation import ImagePullSecretMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_REGISTRY_DEPLOYMENT = "sregym-private-registry"
_REGISTRY_HTPASSWD_SECRET = "sregym-private-registry-htpasswd"
_REGISTRY_USER = "sregym"
_REGISTRY_PASS = "sregympass"
_REGISTRY_PORT = 5000  # port the in-cluster registry Service listens on
_LOCAL_PORT_FORWARD_PORT = 15000  # local port used for `kubectl port-forward` during push

_TARGET_DEPLOYMENT = "recommendation"
_TARGET_CONTAINER = "hotel-reserv-recommendation"
_IMAGE_SEARCH_KEY = "hotel-reservation"  # substring matched against containerd image refs

_SECRET_NAME = "hotel-registry-creds"
_DECOY_SECRET = "nonexistent-pull-creds"

_APP_NAMESPACE = "hotel-reservation"

_INFRA_NAMESPACE = "infra-registry"
_MASTER_SECRET_NAME = "private-registry-creds-master"
_RUNBOOK_CONFIGMAP = "registry-access-runbook"

_DOCKERHUB_BLOCK_DEPLOYMENT = "sregym-dockerhub-block"
_DOCKERHUB_BLOCK_CONFIGMAP = "sregym-dockerhub-block-conf"
_DOCKERHUB_BLOCK_PORT = 80
_DOCKERHUB_BLOCK_NGINX_CONF = """\
server {
    listen 80 default_server;
    location / {
        default_type application/json;
        return 403 '{"errors":[{"code":"DENIED","message":"Mirror policy: public image pulls are not permitted. Use the internal private registry."}]}';
    }
}
"""

# Hostnames a dockerd (cri-dockerd) node resolves to talk to Docker Hub. On these
# nodes we block public pulls by pointing the hostnames at the in-cluster block
# Service via /etc/hosts (TLS handshake fails -> pull error), instead of the
# containerd registry-mirror mechanism used on containerd nodes.
_DOCKERHUB_BLOCK_HOSTS = [
    "docker.io",
    "registry-1.docker.io",
    "auth.docker.io",
    "index.docker.io",
    "production.cloudflare.docker.com",
]
_DOCKERHUB_BLOCK_HOSTS_MARKER = "# sregym-dockerhub-block"


class MissingImagePullSecretHotelReservation(Problem):
    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

        self.problem_id = "missing_image_pull_secret_hotel_reservation"
        self.faulty_service = [_TARGET_DEPLOYMENT]

        self.secret_name = _SECRET_NAME
        self.target_deployment = _TARGET_DEPLOYMENT
        self.target_container = _TARGET_CONTAINER

        self._registry_ip: str | None = None
        self._private_image: str | None = None
        self._source_image: str | None = None
        self._original_image: str | None = None
        self._original_pull_policy: str | None = None
        self._decoy_pod_names: list[str] = []

        self.root_cause = self.build_structured_root_cause(
            component=f"secret/{_SECRET_NAME}",
            namespace=self.namespace,
            description=(
                f"The imagePullSecret '{_SECRET_NAME}' referenced by the "
                f"'{_TARGET_DEPLOYMENT}' Deployment was deleted. "
                f"The pod cannot authenticate to the private in-cluster registry and enters "
                f"ImagePullBackOff. "
                f"Two decoy pods emit the same FailedToRetrieveImagePullSecret warning "
                f"while staying Running — the agent must correlate the warning event with "
                f"actual pod state to identify the one truly broken workload. "
                f"Fix: recreate the imagePullSecret '{_SECRET_NAME}' with valid registry credentials."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ImagePullSecretMitigationOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self) -> bool:
        logger.info("Injecting MissingImagePullSecret fault...")

        # Step 1: start the private registry as an in-cluster Deployment+Service
        self._ensure_namespace(_INFRA_NAMESPACE)
        self._registry_ip = self._start_registry()
        self._private_image = f"{self._registry_ip}:{_REGISTRY_PORT}/hotel-reservation:latest"
        logger.info("Private registry running at %s", self._registry_ip)

        # Step 2: configure containerd on all nodes to allow HTTP pulls from this registry
        self._configure_containerd_insecure(self._registry_ip)

        # Step 3: re-pull hotel-reservation from upstream and push it into the private
        # registry (see _push_image_from_host for why we don't push from containerd directly)
        upstream = self._resolve_upstream_image(exclude_registry_ip=self._registry_ip)
        self._source_image = upstream
        self._push_image_from_host(upstream)
        logger.info("Pushed %s → %s", upstream, self._private_image)

        # Step 4: create a valid imagePullSecret (establishes the healthy baseline)
        self._create_pull_secret()

        # Step 5: repoint the Deployment to the private image + set imagePullPolicy: Always
        # Save the original values so recover_fault() can restore them exactly
        self._original_image, self._original_pull_policy = self._update_deployment(
            image=self._private_image,
            pull_policy="Always",
            add_pull_secret=True,
        )
        logger.info("Deployment repointed to private registry; waiting for pod to stabilise...")
        # Verify the private registry + secret setup works before injecting the fault
        self._wait_for_deployment_ready(timeout=120)

        # Step 6: set up the centralized-secrets discovery path (separate namespace +
        # runbook ConfigMap) so the fix is discoverable rather than requiring a guess
        self._create_infra_namespace_and_secret()
        self._create_runbook_configmap()
        logger.info(
            "Created discovery breadcrumb: ConfigMap '%s' → namespace '%s' / secret '%s'",
            _RUNBOOK_CONFIGMAP,
            _INFRA_NAMESPACE,
            _MASTER_SECRET_NAME,
        )

        # Step 7: block public-registry pulls at the containerd mirror level so an
        # agent can't bypass the secret by repointing the image to Docker Hub
        block_ip = self._start_dockerhub_block()
        self._configure_containerd_dockerhub_block(block_ip)
        logger.info("Public Docker Hub pulls are not allowed")

        # Step 8: inject the fault — delete the imagePullSecret
        try:
            self.core_v1.delete_namespaced_secret(name=_SECRET_NAME, namespace=self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        logger.info("Deleted imagePullSecret '%s'", _SECRET_NAME)

        # Step 9: force a pod restart so the missing secret is immediately observable
        # (with imagePullPolicy: Always, the pod only contacts the registry at start/restart)
        self._delete_deployment_pods()

        # Step 10: create decoy pods — emit warning but stay Running
        self._decoy_pod_names = self._create_decoy_pods()
        logger.info("Created %d decoy pods", len(self._decoy_pod_names))

        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        logger.info("Recovering from MissingImagePullSecret fault...")

        # Step 1: recreate the imagePullSecret so the pod can pull again
        self._create_pull_secret()

        # Step 2: force a pod restart to trigger re-pull with the restored secret
        self._delete_deployment_pods()
        self._wait_for_deployment_ready(timeout=120)
        logger.info("Target pod Running with restored imagePullSecret")

        # Step 3: revert Deployment to original image + imagePullPolicy + no secret reference
        if self._original_image and self._original_pull_policy:
            self._update_deployment(
                image=self._original_image,
                pull_policy=self._original_pull_policy,
                add_pull_secret=False,
            )
        # Wait for the rolling update to the original spec to complete
        self._wait_for_deployment_ready(timeout=120)
        logger.info("Deployment restored to original spec")

        # Step 4: delete decoy pods
        for name in self._decoy_pod_names:
            try:
                self.core_v1.delete_namespaced_pod(
                    name=name,
                    namespace=self.namespace,
                    body=client.V1DeleteOptions(grace_period_seconds=0),
                )
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    raise
        self._decoy_pod_names = []

        # Step 5: delete the imagePullSecret (no longer needed after image revert)
        try:
            self.core_v1.delete_namespaced_secret(name=_SECRET_NAME, namespace=self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

        # Step 6: tear down the Docker Hub block (containerd redirect + responder container)
        self._remove_containerd_dockerhub_block()
        self._stop_dockerhub_block()

        # Step 7: tear down the centralized-secrets discovery scaffolding
        self._teardown_runbook_configmap()
        self._teardown_infra_namespace()

        # Step 8: teardown the private registry
        if self._registry_ip:
            self._remove_containerd_insecure(self._registry_ip)
        self._stop_registry()

        logger.info("Fault recovered.")
        return True

    # ── K8s API helpers (create-or-replace, idempotent) ───────────────────────

    @staticmethod
    def _ensure_namespace(name: str) -> None:
        try:
            client.CoreV1Api().create_namespace(body=client.V1Namespace(metadata=client.V1ObjectMeta(name=name)))
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

    @staticmethod
    def _ensure_secret(secret: client.V1Secret) -> None:
        core_v1 = client.CoreV1Api()
        ns, name = secret.metadata.namespace, secret.metadata.name
        try:
            core_v1.create_namespaced_secret(namespace=ns, body=secret)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                core_v1.replace_namespaced_secret(name=name, namespace=ns, body=secret)
            else:
                raise

    @staticmethod
    def _ensure_config_map(cm: client.V1ConfigMap) -> None:
        core_v1 = client.CoreV1Api()
        ns, name = cm.metadata.namespace, cm.metadata.name
        try:
            core_v1.create_namespaced_config_map(namespace=ns, body=cm)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                core_v1.replace_namespaced_config_map(name=name, namespace=ns, body=cm)
            else:
                raise

    @staticmethod
    def _ensure_deployment(deployment: client.V1Deployment) -> None:
        apps_v1 = client.AppsV1Api()
        ns, name = deployment.metadata.namespace, deployment.metadata.name
        try:
            apps_v1.create_namespaced_deployment(namespace=ns, body=deployment)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                apps_v1.replace_namespaced_deployment(name=name, namespace=ns, body=deployment)
            else:
                raise

    @staticmethod
    def _ensure_service(service: client.V1Service) -> str:
        """Create the Service if missing; return its ClusterIP either way."""
        core_v1 = client.CoreV1Api()
        ns, name = service.metadata.namespace, service.metadata.name
        try:
            created = core_v1.create_namespaced_service(namespace=ns, body=service)
            return created.spec.cluster_ip
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return core_v1.read_namespaced_service(name=name, namespace=ns).spec.cluster_ip
            raise

    @staticmethod
    def _delete_deployment_and_service(name: str, namespace: str) -> None:
        apps_v1 = client.AppsV1Api()
        core_v1 = client.CoreV1Api()
        with contextlib.suppress(client.exceptions.ApiException):
            apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
        with contextlib.suppress(client.exceptions.ApiException):
            core_v1.delete_namespaced_service(name=name, namespace=namespace)

    # ── Per-node access (kind: docker exec, real cluster: one-shot pod) ───────

    @staticmethod
    def _get_cluster_nodes() -> list[str]:
        """Return the names of all nodes in the cluster.

        On Kind, node container names equal the Kubernetes node names, so the
        existing `docker exec`/`docker cp` branches below keep working unchanged
        for any node whose name starts with "kind-".
        """
        return [n.metadata.name for n in client.CoreV1Api().list_node().items]

    @staticmethod
    def _get_node_runtime(node_name: str) -> str:
        """Return "docker" for cri-dockerd nodes, "containerd" otherwise.

        Real clusters provisioned via `scripts/ansible/setup_cluster.yml` use
        Docker + cri-dockerd as the CRI, so image-pull config must go through
        `/etc/docker/daemon.json` + `systemctl restart docker` rather than
        containerd's `certs.d` mechanism.
        """
        node = client.CoreV1Api().read_node(name=node_name)
        runtime_version = (node.status.node_info.container_runtime_version or "") if node.status else ""
        return "docker" if runtime_version.startswith("docker://") else "containerd"

    @staticmethod
    def _run_on_node(node_name: str, script: str, timeout: int = 60) -> str:
        """Run a shell `script` on `node_name` via a one-shot privileged pod.

        Modeled on `kubectl.py:_run_localpv_gc_pod_on_node`: busybox image,
        privileged, host root mounted at /host, tolerates all taints so it can
        land on control-plane nodes too. Runs with hostPID so `nsenter -t 1 ...`
        can reach the host's systemd to restart containerd.
        """
        core_v1 = client.CoreV1Api()
        short_node = node_name.split(".")[0].lower().replace("_", "-")[:40]
        pod_name = f"sregym-node-op-{short_node}-{int(time.time()) % 100000}"[:63]
        namespace = "kube-system"

        pod_body = client.V1Pod(
            metadata=client.V1ObjectMeta(name=pod_name, namespace=namespace, labels={"app": "sregym-node-op"}),
            spec=client.V1PodSpec(
                node_name=node_name,
                restart_policy="Never",
                host_pid=True,
                tolerations=[client.V1Toleration(operator="Exists")],
                automount_service_account_token=False,
                containers=[
                    client.V1Container(
                        name="op",
                        image="busybox:1.36",
                        image_pull_policy="IfNotPresent",
                        command=["sh", "-c", script],
                        security_context=client.V1SecurityContext(privileged=True),
                        volume_mounts=[client.V1VolumeMount(name="host", mount_path="/host")],
                    )
                ],
                volumes=[
                    client.V1Volume(name="host", host_path=client.V1HostPathVolumeSource(path="/", type="Directory")),
                ],
            ),
        )

        with contextlib.suppress(client.exceptions.ApiException):
            core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=0)
        core_v1.create_namespaced_pod(namespace=namespace, body=pod_body)

        try:
            waited, sleep_s, phase = 0, 2, "Pending"
            while waited < timeout:
                pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                phase = (pod.status.phase or "Pending") if pod.status else "Pending"
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(sleep_s)
                waited += sleep_s
            else:
                raise TimeoutError(
                    f"Node-op pod {pod_name} on {node_name} did not finish within {timeout}s (phase={phase})"
                )

            logs = ""
            with contextlib.suppress(client.exceptions.ApiException):
                logs = core_v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            if phase != "Succeeded":
                raise RuntimeError(
                    f"Node-op pod {pod_name} on {node_name} ended with phase={phase}; logs: {logs.strip()[:500]}"
                )
            return logs
        finally:
            with contextlib.suppress(client.exceptions.ApiException):
                core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=0)

    # ── Registry lifecycle ────────────────────────────────────────────────────

    def _start_registry(self) -> str:
        """Start a password-protected registry:2 as a Deployment+Service in
        `_INFRA_NAMESPACE`; return the Service's ClusterIP."""
        htpasswd = self._generate_htpasswd(_REGISTRY_USER, _REGISTRY_PASS)
        self._ensure_secret(
            client.V1Secret(
                metadata=client.V1ObjectMeta(name=_REGISTRY_HTPASSWD_SECRET, namespace=_INFRA_NAMESPACE),
                type="Opaque",
                string_data={"htpasswd": htpasswd},
            )
        )
        self._ensure_deployment(
            client.V1Deployment(
                metadata=client.V1ObjectMeta(
                    name=_REGISTRY_DEPLOYMENT, namespace=_INFRA_NAMESPACE, labels={"app": _REGISTRY_DEPLOYMENT}
                ),
                spec=client.V1DeploymentSpec(
                    replicas=1,
                    selector=client.V1LabelSelector(match_labels={"app": _REGISTRY_DEPLOYMENT}),
                    template=client.V1PodTemplateSpec(
                        metadata=client.V1ObjectMeta(labels={"app": _REGISTRY_DEPLOYMENT}),
                        spec=client.V1PodSpec(
                            containers=[
                                client.V1Container(
                                    name="registry",
                                    image="registry:2",
                                    ports=[client.V1ContainerPort(container_port=_REGISTRY_PORT)],
                                    env=[
                                        client.V1EnvVar(name="REGISTRY_AUTH", value="htpasswd"),
                                        client.V1EnvVar(name="REGISTRY_AUTH_HTPASSWD_REALM", value="SREGym Registry"),
                                        client.V1EnvVar(name="REGISTRY_AUTH_HTPASSWD_PATH", value="/auth/htpasswd"),
                                    ],
                                    volume_mounts=[
                                        client.V1VolumeMount(name="htpasswd", mount_path="/auth", read_only=True)
                                    ],
                                )
                            ],
                            volumes=[
                                client.V1Volume(
                                    name="htpasswd",
                                    secret=client.V1SecretVolumeSource(secret_name=_REGISTRY_HTPASSWD_SECRET),
                                ),
                            ],
                        ),
                    ),
                ),
            )
        )
        cluster_ip = self._ensure_service(
            client.V1Service(
                metadata=client.V1ObjectMeta(name=_REGISTRY_DEPLOYMENT, namespace=_INFRA_NAMESPACE),
                spec=client.V1ServiceSpec(
                    selector={"app": _REGISTRY_DEPLOYMENT},
                    ports=[client.V1ServicePort(port=_REGISTRY_PORT, target_port=_REGISTRY_PORT)],
                ),
            )
        )
        self._wait_for_deployment_ready(_REGISTRY_DEPLOYMENT, _INFRA_NAMESPACE, timeout=120)
        return cluster_ip

    @staticmethod
    def _stop_registry() -> None:
        MissingImagePullSecretHotelReservation._delete_deployment_and_service(_REGISTRY_DEPLOYMENT, _INFRA_NAMESPACE)
        with contextlib.suppress(client.exceptions.ApiException):
            client.CoreV1Api().delete_namespaced_secret(name=_REGISTRY_HTPASSWD_SECRET, namespace=_INFRA_NAMESPACE)

    @staticmethod
    def _generate_htpasswd(user: str, password: str) -> str:
        """Generate a bcrypt htpasswd entry (registry:2 requires bcrypt, not APR1-MD5)."""
        import bcrypt

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        return f"{user}:{hashed.decode()}\n"

    # ── Containerd configuration on cluster nodes ─────────────────────────────

    def _configure_containerd_insecure(self, registry_ip: str) -> None:
        """Configure all nodes to pull from the private registry over plain HTTP."""
        addr = f"{registry_ip}:{_REGISTRY_PORT}"
        certs_dir = f"/etc/containerd/certs.d/{addr}"
        hosts_toml = f'server = "http://{addr}"\n\n[host."http://{addr}"]\n  capabilities = ["pull", "resolve"]\n'
        config_path_snippet = (
            '\n[plugins."io.containerd.grpc.v1.cri".registry]\n  config_path = "/etc/containerd/certs.d"\n'
        )
        restarted = []
        for node in self._get_cluster_nodes():
            if node.startswith("kind-"):
                subprocess.run(["docker", "exec", node, "mkdir", "-p", certs_dir], check=True)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
                    f.write(hosts_toml)
                    tmp = f.name
                subprocess.run(["docker", "cp", tmp, f"{node}:{certs_dir}/hosts.toml"], check=True)
                os.unlink(tmp)
                already = subprocess.run(
                    ["docker", "exec", node, "grep", "-q", "config_path", "/etc/containerd/config.toml"],
                    capture_output=True,
                )
                if already.returncode != 0:
                    # -i is required so docker exec attaches stdin and tee receives the input
                    subprocess.run(
                        ["docker", "exec", "-i", node, "tee", "-a", "/etc/containerd/config.toml"],
                        input=config_path_snippet,
                        text=True,
                        capture_output=True,
                        check=True,
                    )
                    subprocess.run(
                        ["docker", "exec", node, "systemctl", "restart", "containerd"],
                        check=True,
                    )
                    restarted.append(node)
            elif self._get_node_runtime(node) == "docker":
                # cri-dockerd node: dockerd reads /etc/docker/daemon.json, not
                # containerd's certs.d.
                script = self._daemon_json_insecure_registry_script(addr)
                if "RELOADED" in self._run_on_node(node, script):
                    restarted.append(node)
            else:
                script = (
                    f"mkdir -p /host{certs_dir} && printf '%s' {shlex.quote(hosts_toml)} > /host{certs_dir}/hosts.toml && "
                    f"if ! grep -q config_path /host/etc/containerd/config.toml; then "
                    f"printf '%s' {shlex.quote(config_path_snippet)} >> /host/etc/containerd/config.toml && "
                    f"nsenter -t 1 -m -u -n -i -- systemctl restart containerd && echo RESTARTED; fi"
                )
                if "RESTARTED" in self._run_on_node(node, script):
                    restarted.append(node)
        if restarted:
            logger.info(
                "Restarted containerd on %d node(s) to activate config_path; waiting 10s...",
                len(restarted),
            )
            time.sleep(10)

    def _remove_containerd_insecure(self, registry_ip: str) -> None:
        addr = f"{registry_ip}:{_REGISTRY_PORT}"
        certs_dir = f"/etc/containerd/certs.d/{addr}"
        for node in self._get_cluster_nodes():
            if node.startswith("kind-"):
                subprocess.run(
                    ["docker", "exec", node, "rm", "-rf", certs_dir],
                    capture_output=True,
                )
            elif self._get_node_runtime(node) == "docker":
                with contextlib.suppress(Exception):
                    self._run_on_node(node, self._daemon_json_remove_insecure_registry_script(addr))
            else:
                with contextlib.suppress(Exception):
                    self._run_on_node(node, f"rm -rf /host{certs_dir}")

    _DOCKER_RELOAD_CMD = "nsenter -t 1 -m -u -n -i -- systemctl reload docker &&"

    @staticmethod
    def _daemon_json_insecure_registry_script(addr: str) -> str:
        """Write /etc/docker/daemon.json with "insecure-registries": [addr] and
        "live-restore": true, then reload docker (cri-dockerd nodes only).
        """
        body = json.dumps({"insecure-registries": [addr], "live-restore": True})
        return (
            "f=/host/etc/docker/daemon.json; "
            f"want={shlex.quote(body)}; "
            f'if [ -f "$f" ] && [ "$(cat "$f")" = "$want" ]; then echo "daemon.json already correct, skipping"; '
            f'else printf "%s" "$want" > "$f" && '
            f"{MissingImagePullSecretHotelReservation._DOCKER_RELOAD_CMD} echo RELOADED; fi"
        )

    @staticmethod
    def _daemon_json_remove_insecure_registry_script(addr: str) -> str:
        """Reverse of `_daemon_json_insecure_registry_script`: if daemon.json contains
        an "insecure-registries" entry (regardless of which addr -- a prior run may
        have written a different one), replace it with just `{"live-restore": true}`
        and reload docker."""
        del addr  # unused: removal is keyed off the presence of "insecure-registries", not a specific addr
        reverted = json.dumps({"live-restore": True})
        return (
            "f=/host/etc/docker/daemon.json; "
            f'if [ -f "$f" ] && grep -q "insecure-registries" "$f"; then '
            f"printf '%s' {shlex.quote(reverted)} > \"$f\" && "
            f"{MissingImagePullSecretHotelReservation._DOCKER_RELOAD_CMD} echo RELOADED; fi"
        )

    def _configure_containerd_dockerhub_block(self, block_ip: str) -> None:
        """Redirect docker.io pulls to the 403-responder via containerd's mirror mechanism."""
        addr = f"{block_ip}:{_DOCKERHUB_BLOCK_PORT}"
        hosts_toml = f'server = "http://{addr}"\n\n[host."http://{addr}"]\n  capabilities = ["pull", "resolve"]\n'
        certs_dir = "/etc/containerd/certs.d/docker.io"
        for node in self._get_cluster_nodes():
            if node.startswith("kind-"):
                subprocess.run(["docker", "exec", node, "mkdir", "-p", certs_dir], check=True)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
                    f.write(hosts_toml)
                    tmp = f.name
                subprocess.run(["docker", "cp", tmp, f"{node}:{certs_dir}/hosts.toml"], check=True)
                os.unlink(tmp)
            elif self._get_node_runtime(node) == "docker":
                # Point the Docker Hub hostnames at the block Service via /etc/hosts to block public pulls.
                hosts_lines = "\n".join(
                    f"{block_ip} {h} {_DOCKERHUB_BLOCK_HOSTS_MARKER}" for h in _DOCKERHUB_BLOCK_HOSTS
                )
                script = f"printf '%s\\n' {shlex.quote(hosts_lines)} >> /host/etc/hosts"
                self._run_on_node(node, script)
            else:
                script = (
                    f"mkdir -p /host{certs_dir} && printf '%s' {shlex.quote(hosts_toml)} > /host{certs_dir}/hosts.toml"
                )
                self._run_on_node(node, script)

    @staticmethod
    def _remove_containerd_dockerhub_block() -> None:
        certs_dir = "/etc/containerd/certs.d/docker.io"
        for node in MissingImagePullSecretHotelReservation._get_cluster_nodes():
            if node.startswith("kind-"):
                subprocess.run(
                    ["docker", "exec", node, "rm", "-rf", certs_dir],
                    capture_output=True,
                )
            elif MissingImagePullSecretHotelReservation._get_node_runtime(node) == "docker":
                with contextlib.suppress(Exception):
                    script = f"sed -i '/{_DOCKERHUB_BLOCK_HOSTS_MARKER.replace('/', r'\\/')}/d' /host/etc/hosts"
                    MissingImagePullSecretHotelReservation._run_on_node(node, script)
            else:
                with contextlib.suppress(Exception):
                    MissingImagePullSecretHotelReservation._run_on_node(node, f"rm -rf /host{certs_dir}")

    def _start_dockerhub_block(self) -> str:
        """Start the nginx Deployment+Service that returns 403 Forbidden for any
        request; return the Service's ClusterIP."""
        self._ensure_config_map(
            client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=_DOCKERHUB_BLOCK_CONFIGMAP, namespace=_INFRA_NAMESPACE),
                data={"default.conf": _DOCKERHUB_BLOCK_NGINX_CONF},
            )
        )
        self._ensure_deployment(
            client.V1Deployment(
                metadata=client.V1ObjectMeta(
                    name=_DOCKERHUB_BLOCK_DEPLOYMENT,
                    namespace=_INFRA_NAMESPACE,
                    labels={"app": _DOCKERHUB_BLOCK_DEPLOYMENT},
                ),
                spec=client.V1DeploymentSpec(
                    replicas=1,
                    selector=client.V1LabelSelector(match_labels={"app": _DOCKERHUB_BLOCK_DEPLOYMENT}),
                    template=client.V1PodTemplateSpec(
                        metadata=client.V1ObjectMeta(labels={"app": _DOCKERHUB_BLOCK_DEPLOYMENT}),
                        spec=client.V1PodSpec(
                            containers=[
                                client.V1Container(
                                    name="nginx",
                                    image="nginx:alpine",
                                    ports=[client.V1ContainerPort(container_port=_DOCKERHUB_BLOCK_PORT)],
                                    volume_mounts=[
                                        client.V1VolumeMount(
                                            name="conf", mount_path="/etc/nginx/conf.d", read_only=True
                                        )
                                    ],
                                )
                            ],
                            volumes=[
                                client.V1Volume(
                                    name="conf",
                                    config_map=client.V1ConfigMapVolumeSource(name=_DOCKERHUB_BLOCK_CONFIGMAP),
                                ),
                            ],
                        ),
                    ),
                ),
            )
        )
        cluster_ip = self._ensure_service(
            client.V1Service(
                metadata=client.V1ObjectMeta(name=_DOCKERHUB_BLOCK_DEPLOYMENT, namespace=_INFRA_NAMESPACE),
                spec=client.V1ServiceSpec(
                    selector={"app": _DOCKERHUB_BLOCK_DEPLOYMENT},
                    ports=[client.V1ServicePort(port=_DOCKERHUB_BLOCK_PORT, target_port=_DOCKERHUB_BLOCK_PORT)],
                ),
            )
        )
        self._wait_for_deployment_ready(_DOCKERHUB_BLOCK_DEPLOYMENT, _INFRA_NAMESPACE, timeout=120)
        return cluster_ip

    @staticmethod
    def _stop_dockerhub_block() -> None:
        MissingImagePullSecretHotelReservation._delete_deployment_and_service(
            _DOCKERHUB_BLOCK_DEPLOYMENT, _INFRA_NAMESPACE
        )
        with contextlib.suppress(client.exceptions.ApiException):
            client.CoreV1Api().delete_namespaced_config_map(name=_DOCKERHUB_BLOCK_CONFIGMAP, namespace=_INFRA_NAMESPACE)

    @staticmethod
    def _cleanup_duplicate_recommendation_replicasets() -> None:
        """A prior run's rolling update (image repoint <-> revert) can leave an old
        ReplicaSet for `_TARGET_DEPLOYMENT` with `replicas > 0`, scale any others
        besides the primary pod down to 0.
        """
        apps_v1 = client.AppsV1Api()
        rs_list = apps_v1.list_namespaced_replica_set(
            namespace=_APP_NAMESPACE, label_selector=f"io.kompose.service={_TARGET_DEPLOYMENT}"
        )
        owned = [
            rs
            for rs in rs_list.items
            if any(
                o.kind == "Deployment" and o.name == _TARGET_DEPLOYMENT for o in (rs.metadata.owner_references or [])
            )
        ]
        if len(owned) < 2:
            return

        def revision(rs: client.V1ReplicaSet) -> int:
            return int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))

        owned.sort(key=revision, reverse=True)
        for rs in owned[1:]:
            if (rs.spec.replicas or 0) > 0:
                apps_v1.patch_namespaced_replica_set_scale(
                    name=rs.metadata.name, namespace=_APP_NAMESPACE, body={"spec": {"replicas": 0}}
                )
                logger.info(
                    "Scaled down stale ReplicaSet '%s' for '%s' (revision %d) to 0",
                    rs.metadata.name,
                    _TARGET_DEPLOYMENT,
                    revision(rs),
                )

    @staticmethod
    def cleanup_leftovers() -> None:
        """Remove MissingImagePullSecret infrastructure left behind by an interrupted run (e.g. Ctrl+C before recover_fault runs)."""
        MissingImagePullSecretHotelReservation._remove_containerd_dockerhub_block()
        MissingImagePullSecretHotelReservation._stop_dockerhub_block()
        MissingImagePullSecretHotelReservation._stop_registry()
        MissingImagePullSecretHotelReservation._cleanup_duplicate_recommendation_replicasets()

    def _create_infra_namespace_and_secret(self) -> None:
        """Create the 'platform team' namespace holding the source-of-truth registry
        credentials, stored as raw fields (not a pre-built dockerconfigjson) so the
        agent must still construct a correct imagePullSecret itself.
        """
        self._ensure_namespace(_INFRA_NAMESPACE)

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=_MASTER_SECRET_NAME, namespace=_INFRA_NAMESPACE),
            type="Opaque",
            string_data={
                "registry": f"{self._registry_ip}:{_REGISTRY_PORT}",
                "username": _REGISTRY_USER,
                "password": _REGISTRY_PASS,
            },
        )
        self._ensure_secret(secret)

    def _create_runbook_configmap(self) -> None:
        """Leave a hint ConfigMap pointing at the password registry and the namespace"""
        runbook = (
            "# Image Registry Access\n"
            "\n"
            "This service pulls its container image from the internal private registry.\n"
            "Pull credentials are centrally managed by the platform team in the\n"
            f"`{_INFRA_NAMESPACE}` namespace (secret: `{_MASTER_SECRET_NAME}`).\n"
            "\n"
            "To restore access:\n"
            f"1. kubectl get secret {_MASTER_SECRET_NAME} -n {_INFRA_NAMESPACE} -o yaml\n"
            "2. Decode the 'registry', 'username', and 'password' fields.\n"
            f"3. Recreate the imagePullSecret `{_SECRET_NAME}` in this namespace using\n"
            "   those credentials and re-attach it to the Deployment.\n"
        )
        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=_RUNBOOK_CONFIGMAP, namespace=self.namespace),
            data={"README.md": runbook},
        )
        try:
            self.core_v1.create_namespaced_config_map(namespace=self.namespace, body=cm)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                self.core_v1.replace_namespaced_config_map(name=_RUNBOOK_CONFIGMAP, namespace=self.namespace, body=cm)
            else:
                raise

    def _teardown_runbook_configmap(self) -> None:
        try:
            self.core_v1.delete_namespaced_config_map(name=_RUNBOOK_CONFIGMAP, namespace=self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

    def _teardown_infra_namespace(self) -> None:
        try:
            self.core_v1.delete_namespace(name=_INFRA_NAMESPACE)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

    def _resolve_upstream_image(self, exclude_registry_ip: str | None = None) -> str:
        deploy = self.apps_v1.read_namespaced_deployment(name=_TARGET_DEPLOYMENT, namespace=self.namespace)
        containers = deploy.spec.template.spec.containers or []
        target = next(
            (c for c in containers if c.name == _TARGET_CONTAINER),
            containers[0] if containers else None,
        )
        if not target or not target.image:
            raise RuntimeError(
                f"Cannot determine upstream image: container '{_TARGET_CONTAINER}' "
                f"not found or has no image in deployment '{_TARGET_DEPLOYMENT}'."
            )
        image = target.image
        if exclude_registry_ip and image.startswith(f"{exclude_registry_ip}:"):
            raise RuntimeError(
                f"Deployment '{_TARGET_DEPLOYMENT}' is already pointing at the private "
                f"registry ({image}). Call recover_fault() before inject_fault()."
            )
        return image

    def _push_image_from_host(self, upstream_ref: str) -> None:
        """Re-pull the image from upstream and push it to the in-cluster private
        registry via `kubectl port-forward` (works on kind and real clusters alike,
        as it only needs API server access)."""
        subprocess.run(["docker", "pull", upstream_ref], check=True)

        local_port = _LOCAL_PORT_FORWARD_PORT
        port_forward = subprocess.Popen(
            [
                "kubectl",
                "port-forward",
                "-n",
                _INFRA_NAMESPACE,
                f"svc/{_REGISTRY_DEPLOYMENT}",
                f"{local_port}:{_REGISTRY_PORT}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for the port-forward to actually accept connections. Over an
            # SSH-tunneled apiserver (e.g. the AWS cluster in
            # `.agent/actual-clusters.md`), establishing the forward can take much
            # longer than a fixed 2s sleep, leading to "connection refused" on the
            # first docker command.
            for _ in range(60):
                if port_forward.poll() is not None:
                    raise RuntimeError(f"kubectl port-forward exited early with code {port_forward.returncode}")
                with contextlib.suppress(OSError), socket.create_connection(("localhost", local_port), timeout=1):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(f"kubectl port-forward did not become ready on localhost:{local_port}")

            host_tag = f"localhost:{local_port}/hotel-reservation:latest"
            subprocess.run(["docker", "tag", upstream_ref, host_tag], check=True)
            subprocess.run(
                [
                    "docker",
                    "login",
                    f"localhost:{local_port}",
                    "--username",
                    _REGISTRY_USER,
                    "--password-stdin",
                ],
                input=_REGISTRY_PASS,
                text=True,
                check=True,
            )
            subprocess.run(["docker", "push", host_tag], check=True)
        finally:
            port_forward.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                port_forward.wait(timeout=10)

    def _create_pull_secret(self) -> None:
        """Create or replace the docker-registry imagePullSecret."""
        auth = base64.b64encode(f"{_REGISTRY_USER}:{_REGISTRY_PASS}".encode()).decode()
        dockerconfig = json.dumps(
            {
                "auths": {
                    f"{self._registry_ip}:{_REGISTRY_PORT}": {
                        "username": _REGISTRY_USER,
                        "password": _REGISTRY_PASS,
                        "auth": auth,
                    }
                }
            }
        )
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=_SECRET_NAME, namespace=self.namespace),
            type="kubernetes.io/dockerconfigjson",
            data={".dockerconfigjson": base64.b64encode(dockerconfig.encode()).decode()},
        )
        try:
            self.core_v1.create_namespaced_secret(namespace=self.namespace, body=secret)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                self.core_v1.replace_namespaced_secret(name=_SECRET_NAME, namespace=self.namespace, body=secret)
            else:
                raise

    def _update_deployment(
        self,
        image: str,
        pull_policy: str,
        add_pull_secret: bool,
    ) -> tuple[str, str]:
        """Read-modify-replace the target Deployment; return (original_image, original_policy)."""
        for attempt in range(3):
            deploy = self.apps_v1.read_namespaced_deployment(name=_TARGET_DEPLOYMENT, namespace=self.namespace)
            containers = deploy.spec.template.spec.containers or []

            target = next(
                (c for c in containers if c.name == _TARGET_CONTAINER),
                containers[0] if containers else None,
            )
            original_image = target.image if target else image
            original_policy = target.image_pull_policy if target else "IfNotPresent"

            # Patch the target container in-place
            for c in containers:
                if c.name == _TARGET_CONTAINER:
                    c.image = image
                    c.image_pull_policy = pull_policy
                    break

            deploy.spec.template.spec.image_pull_secrets = (
                [client.V1LocalObjectReference(name=_SECRET_NAME)] if add_pull_secret else []
            )

            try:  # This has been added to avoid race conditions between our read and PUT and rollout controller's version update
                self.apps_v1.replace_namespaced_deployment(
                    name=_TARGET_DEPLOYMENT, namespace=self.namespace, body=deploy
                )
                return original_image, original_policy
            except client.exceptions.ApiException as e:
                if e.status == 409 and attempt < 2:
                    time.sleep(1)
                    continue
                raise

    def _delete_deployment_pods(self) -> None:
        """Force-delete the target Deployment's pods to trigger an immediate kubelet re-pull."""
        pods = self.core_v1.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=f"io.kompose.service={_TARGET_DEPLOYMENT}",
        )
        for pod in pods.items:
            try:
                self.core_v1.delete_namespaced_pod(
                    name=pod.metadata.name,
                    namespace=self.namespace,
                    body=client.V1DeleteOptions(grace_period_seconds=0),
                )
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    raise

    def _wait_for_deployment_ready(
        self, deployment_name: str = _TARGET_DEPLOYMENT, namespace: str | None = None, timeout: int = 120
    ) -> None:
        """Poll until the rollout is fully complete (replicates `kubectl rollout status`)."""
        namespace = namespace or self.namespace
        deadline = time.time() + timeout
        while time.time() < deadline:
            deploy = self.apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
            desired = deploy.spec.replicas or 1
            status = deploy.status
            observed_current = (status.observed_generation or 0) >= (deploy.metadata.generation or 0)
            updated = status.updated_replicas or 0
            total = status.replicas or 0
            ready = status.ready_replicas or 0
            if observed_current and updated == desired and total == desired and ready == desired:
                return
            time.sleep(3)
        logger.warning("Deployment '%s' rollout not complete after %ds", deployment_name, timeout)

    def _create_decoy_pods(self) -> list[str]:
        """Create bare pods that emit FailedToRetrieveImagePullSecret but stay Running."""
        names = []
        for i in range(2):
            name = f"hotel-res-logger-{i}"
            pod = client.V1Pod(
                metadata=client.V1ObjectMeta(
                    name=name,
                    namespace=self.namespace,
                    labels={"sregym-decoy": "true"},
                ),
                spec=client.V1PodSpec(
                    image_pull_secrets=[client.V1LocalObjectReference(name=_DECOY_SECRET)],
                    restart_policy="Always",
                    containers=[
                        client.V1Container(
                            name="decoy",
                            # Use the same image ref that's actually in the Kind cache —
                            # discovered dynamically in inject_fault() via _resolve_upstream_image()
                            image=self._source_image or _IMAGE_SEARCH_KEY,
                            image_pull_policy="IfNotPresent",
                            command=["sleep", "3600"],
                        )
                    ],
                ),
            )
            try:
                self.core_v1.create_namespaced_pod(namespace=self.namespace, body=pod)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise
            names.append(name)
        return names
