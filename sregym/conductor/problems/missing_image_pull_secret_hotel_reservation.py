import base64
import json
import logging
import os
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

_REGISTRY_CONTAINER = "sregym-private-registry"
_REGISTRY_USER = "sregym"
_REGISTRY_PASS = "sregympass"
_REGISTRY_PORT = 5000  # port on the kind Docker bridge network
_REGISTRY_HOST_PORT = 5001  # port mapped on the host for docker push

_TARGET_DEPLOYMENT = "recommendation"
_TARGET_CONTAINER = "hotel-reserv-recommendation"
_IMAGE_SEARCH_KEY = "hotel-reservation"  # substring matched against containerd image refs

_SECRET_NAME = "hotel-registry-creds"
_DECOY_SECRET = "nonexistent-pull-creds"

_INFRA_NAMESPACE = "infra-registry"
_MASTER_SECRET_NAME = "private-registry-creds-master"
_RUNBOOK_CONFIGMAP = "registry-access-runbook"

_DOCKERHUB_BLOCK_CONTAINER = "sregym-dockerhub-block"
_DOCKERHUB_MIRROR_ALIAS = "dockerhub-mirror.internal"
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

        # Step 1: start the private registry on the kind Docker bridge network
        self._registry_ip = self._start_registry()
        self._private_image = f"{self._registry_ip}:{_REGISTRY_PORT}/hotel-reservation:latest"
        logger.info("Private registry running at %s", self._registry_ip)

        # Step 2: configure containerd on all Kind nodes to allow HTTP pulls from this registry
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
        self._start_dockerhub_block()
        self._configure_containerd_dockerhub_block()
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

    # ── Registry lifecycle ────────────────────────────────────────────────────

    def _start_registry(self) -> str:
        """Start a password-protected registry:2 on the kind Docker bridge; return its IP."""
        htpasswd = self._generate_htpasswd(_REGISTRY_USER, _REGISTRY_PASS)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".htpasswd", delete=False, dir="/tmp") as f:
            f.write(htpasswd)
            htpasswd_path = f.name
        os.chmod(htpasswd_path, 0o644)

        # Remove any leftover container from a previous (failed) run
        subprocess.run(["docker", "rm", "-f", _REGISTRY_CONTAINER], capture_output=True)

        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                _REGISTRY_CONTAINER,
                "--network",
                "kind",
                "-p",
                f"{_REGISTRY_HOST_PORT}:{_REGISTRY_PORT}",
                "-e",
                "REGISTRY_AUTH=htpasswd",
                "-e",
                "REGISTRY_AUTH_HTPASSWD_REALM=SREGym Registry",
                "-e",
                "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
                "-v",
                f"{htpasswd_path}:/auth/htpasswd:ro",
                "registry:2",
            ],
            check=True,
        )
        time.sleep(2)  # allow registry to initialise before clients connect

        result = subprocess.run(
            [
                "docker",
                "inspect",
                _REGISTRY_CONTAINER,
                "--format",
                "{{.NetworkSettings.Networks.kind.IPAddress}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        ip = result.stdout.strip()
        if not ip:
            raise RuntimeError(
                f"Could not determine IP of registry container '{_REGISTRY_CONTAINER}' "
                "on the kind network. Is the Kind cluster running?"
            )
        return ip

    @staticmethod
    def _stop_registry() -> None:
        subprocess.run(["docker", "rm", "-f", _REGISTRY_CONTAINER], capture_output=True)

    @staticmethod
    def _generate_htpasswd(user: str, password: str) -> str:
        """Generate a bcrypt htpasswd entry (registry:2 requires bcrypt, not APR1-MD5)."""
        import bcrypt

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        return f"{user}:{hashed.decode()}\n"

    # ── Containerd configuration on Kind nodes ────────────────────────────────

    @staticmethod
    def _get_kind_nodes() -> list[str]:
        """Return container names of all nodes in the 'kind' Kind cluster."""
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=io.x-k8s.kind.cluster=kind",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return [n.strip() for n in result.stdout.strip().splitlines() if n.strip()]

    def _configure_containerd_insecure(self, registry_ip: str) -> None:
        """Configure all Kind nodes to pull from the private registry over plain HTTP."""
        addr = f"{registry_ip}:{_REGISTRY_PORT}"
        certs_dir = f"/etc/containerd/certs.d/{addr}"
        hosts_toml = f'server = "http://{addr}"\n\n[host."http://{addr}"]\n  capabilities = ["pull", "resolve"]\n'
        config_path_snippet = (
            '\n[plugins."io.containerd.grpc.v1.cri".registry]\n  config_path = "/etc/containerd/certs.d"\n'
        )
        restarted = []
        for node in self._get_kind_nodes():
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
        if restarted:
            logger.info(
                "Restarted containerd on %d node(s) to activate config_path; waiting 10s...",
                len(restarted),
            )
            time.sleep(10)

    def _remove_containerd_insecure(self, registry_ip: str) -> None:
        addr = f"{registry_ip}:{_REGISTRY_PORT}"
        certs_dir = f"/etc/containerd/certs.d/{addr}"
        for node in self._get_kind_nodes():
            subprocess.run(
                ["docker", "exec", node, "rm", "-rf", certs_dir],
                capture_output=True,
            )

    def _configure_containerd_dockerhub_block(self) -> None:
        """Redirect docker.io pulls to the 403-responder via containerd's mirror mechanism."""
        addr = f"{_DOCKERHUB_MIRROR_ALIAS}:{_DOCKERHUB_BLOCK_PORT}"
        hosts_toml = f'server = "http://{addr}"\n\n[host."http://{addr}"]\n  capabilities = ["pull", "resolve"]\n'
        for node in self._get_kind_nodes():
            certs_dir = "/etc/containerd/certs.d/docker.io"
            subprocess.run(["docker", "exec", node, "mkdir", "-p", certs_dir], check=True)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
                f.write(hosts_toml)
                tmp = f.name
            subprocess.run(["docker", "cp", tmp, f"{node}:{certs_dir}/hosts.toml"], check=True)
            os.unlink(tmp)

    @staticmethod
    def _remove_containerd_dockerhub_block() -> None:
        certs_dir = "/etc/containerd/certs.d/docker.io"
        for node in MissingImagePullSecretHotelReservation._get_kind_nodes():
            subprocess.run(
                ["docker", "exec", node, "rm", "-rf", certs_dir],
                capture_output=True,
            )

    def _start_dockerhub_block(self) -> None:
        """Start the nginx container that returns 403 Forbidden for any request"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False, dir="/tmp") as f:
            f.write(_DOCKERHUB_BLOCK_NGINX_CONF)
            conf_path = f.name
        os.chmod(conf_path, 0o644)

        subprocess.run(["docker", "rm", "-f", _DOCKERHUB_BLOCK_CONTAINER], capture_output=True)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                _DOCKERHUB_BLOCK_CONTAINER,
                "--network",
                "kind",
                "--network-alias",
                _DOCKERHUB_MIRROR_ALIAS,
                "-v",
                f"{conf_path}:/etc/nginx/conf.d/default.conf:ro",
                "nginx:alpine",
            ],
            check=True,
        )
        time.sleep(2)

    @staticmethod
    def _stop_dockerhub_block() -> None:
        subprocess.run(["docker", "rm", "-f", _DOCKERHUB_BLOCK_CONTAINER], capture_output=True)

    @staticmethod
    def cleanup_leftovers() -> None:
        """Remove MissingImagePullSecret infrastructure left behind by an interrupted run (e.g. Ctrl+C before recover_fault runs)."""
        MissingImagePullSecretHotelReservation._remove_containerd_dockerhub_block()
        MissingImagePullSecretHotelReservation._stop_dockerhub_block()
        MissingImagePullSecretHotelReservation._stop_registry()

    def _create_infra_namespace_and_secret(self) -> None:
        """Create the 'platform team' namespace holding the source-of-truth registry
        credentials, stored as raw fields (not a pre-built dockerconfigjson) so the
        agent must still construct a correct imagePullSecret itself.
        """
        ns = client.V1Namespace(metadata=client.V1ObjectMeta(name=_INFRA_NAMESPACE))
        try:
            self.core_v1.create_namespace(body=ns)
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=_MASTER_SECRET_NAME, namespace=_INFRA_NAMESPACE),
            type="Opaque",
            string_data={
                "registry": f"{self._registry_ip}:{_REGISTRY_PORT}",
                "username": _REGISTRY_USER,
                "password": _REGISTRY_PASS,
            },
        )
        try:
            self.core_v1.create_namespaced_secret(namespace=_INFRA_NAMESPACE, body=secret)
        except client.exceptions.ApiException as e:
            if e.status == 409:
                self.core_v1.replace_namespaced_secret(
                    name=_MASTER_SECRET_NAME, namespace=_INFRA_NAMESPACE, body=secret
                )
            else:
                raise

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
        """Re-pull the image from upstream and push it to the private registry via host Docker"""
        subprocess.run(["docker", "pull", upstream_ref], check=True)

        host_tag = f"localhost:{_REGISTRY_HOST_PORT}/hotel-reservation:latest"
        subprocess.run(["docker", "tag", upstream_ref, host_tag], check=True)
        subprocess.run(
            [
                "docker",
                "login",
                f"localhost:{_REGISTRY_HOST_PORT}",
                "--username",
                _REGISTRY_USER,
                "--password-stdin",
            ],
            input=_REGISTRY_PASS,
            text=True,
            check=True,
        )
        subprocess.run(["docker", "push", host_tag], check=True)

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

    def _wait_for_deployment_ready(self, timeout: int = 120) -> None:
        """Poll until the rollout is fully complete (replicates `kubectl rollout status`)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            deploy = self.apps_v1.read_namespaced_deployment(name=_TARGET_DEPLOYMENT, namespace=self.namespace)
            desired = deploy.spec.replicas or 1
            status = deploy.status
            observed_current = (status.observed_generation or 0) >= (deploy.metadata.generation or 0)
            updated = status.updated_replicas or 0
            total = status.replicas or 0
            ready = status.ready_replicas or 0
            if observed_current and updated == desired and total == desired and ready == desired:
                return
            time.sleep(3)
        logger.warning("Deployment '%s' rollout not complete after %ds", _TARGET_DEPLOYMENT, timeout)

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
