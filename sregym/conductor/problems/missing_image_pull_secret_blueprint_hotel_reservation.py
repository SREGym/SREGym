import base64
import contextlib
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time

import urllib3
from kubernetes import client, config

from sregym.conductor.oracles.image_pull_secret_mitigation import ImagePullSecretMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_REGISTRY_DEPLOYMENT = "docker-private-registry"
_REGISTRY_HTPASSWD_SECRET = "docker-private-registry-htpasswd"
_REGISTRY_USER = "admin"
_REGISTRY_PASS = "portable_panda123"
_REGISTRY_PORT = 5000  # port the in-cluster registry Service listens on
_LOCAL_PORT_FORWARD_PORT = 15000  # local port used for `kubectl port-forward` during push

# HTTP statuses that indicate a transient/overloaded control plane (worth retrying) rather
# than a definitive client error. A flapping apiserver returns 504 ("request did not
# complete within the allotted timeout") on writes; 429/5xx are similar.
_TRANSIENT_API_STATUSES = frozenset({429, 500, 502, 503, 504})

# Fault target. The original hotel-reservation problem had a structural flaw: all 8 Go
# services share ONE image (`yinfangchen/hotelreservation:latest`), so gating/purging it
# bricked unrelated services. The Blueprint-compiled hotel reservation app fixes this —
# every app service renders a UNIQUE image (`777lefty/docker-<service>-container:latest`,
# e.g. `777lefty/docker-geo-service-container:latest`). Each unique repo is cached
# separately on the node, so gating / purging the target's image is fully isolated — no
# collateral on any sibling service.
_TARGET_DEPLOYMENT = "geo-service"
_TARGET_CONTAINER = "geo-service-container"  # Blueprint convention: f"{deployment}-container"
_TARGET_POD_LABEL = "io.kompose.service"  # kompose-generated pod selector label key
_PRIVATE_IMAGE_PATH = "blueprint-hotel-reservation/geo-service"  # repo path in the private registry

# Peer Blueprint services used ONLY to derive the target's canonical (pre-fault) image at
# recovery time. Each has its OWN unique repo (`777lefty/docker-<svc>-container:latest`),
# so we recover the target's image by taking a peer's image and swapping
# `docker-<peer>-container` → `docker-geo-service-container`. This needs nothing off-cluster
# and is not a fault fingerprint (the agent can already read every Deployment's image).
# NOTE: db/cache pods (mongo / memcached) are deliberately excluded — they share generic
# images and don't follow the unique-per-service naming pattern.
_PEER_APP_SERVICES = (
    "frontend-service",
    "profile-service",
    "rate-service",
    "recomd-service",
    "reserv-service",
    "search-service",
    "user-service",
)

_SECRET_NAME = "geo-service-registry-creds"
_DECOY_SECRET = "nonexistent-pull-creds"
# Image for the decoy "logger" pods. Must be node-cached and from a non-blocked
# registry: the pause image is the pod-sandbox image present on every node, runs
# forever, and lives on registry.k8s.io (this fault only blocks docker.io).
_DECOY_IMAGE = "registry.k8s.io/pause:3.9"

_APP_NAMESPACE = "blueprint-hotel-reservation"

# NOTE: we deliberately do NOT stamp the original image / pull policy onto the target
# Deployment. Any such annotation leaks the fix to the agent (it can read the original
# public image ref + policy and "revert" instead of solving). Crash-recovery
# (cleanup_leftovers) instead derives the original image/policy from a sibling service's
# image (`_canonical_target_image_and_policy`) and detects an active fault purely from
# cluster signals (image points at the private registry / the injected pull secret is
# referenced).

_INFRA_NAMESPACE = "infra-registry"
_MASTER_SECRET_NAME = "private-registry-creds-master"
_RUNBOOK_CONFIGMAP = "registry-access-runbook"

_DOCKERHUB_BLOCK_DEPLOYMENT = "registry-mirror"
_DOCKERHUB_BLOCK_CONFIGMAP = "registry-mirror-conf"
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

# Public registries blocked at the containerd mirror level so the fault can't be bypassed
# by repointing the Deployment back to the public image. Blueprint images live on Docker
# Hub (`777lefty/...`), so blocking docker.io alone closes the repoint-to-public cheat
# (no ghcr.io needed, unlike the astronomy-shop variant). Caveat (same as the
# hotel-reservation problem): if another service is rescheduled to a node that doesn't
# already have its image cached during the episode, its pull would also be blocked.
# Sibling images are already warm on their nodes, so this only bites on a fresh reschedule.
_BLOCK_REGISTRY_HOSTS = ["docker.io"]

# Hostnames a dockerd (cri-dockerd) node resolves to talk to the blocked registries. On
# these nodes we block public pulls by pointing the hostnames at the in-cluster block
# Service via /etc/hosts (TLS handshake fails -> pull error), instead of the containerd
# registry-mirror mechanism used on containerd nodes.
_DOCKERHUB_BLOCK_HOSTS = [
    "docker.io",
    "registry-1.docker.io",
    "auth.docker.io",
    "index.docker.io",
    "production.cloudflare.docker.com",
]
_DOCKERHUB_BLOCK_HOSTS_MARKER = "# registry-mirror"


class MissingImagePullSecretBlueprintHotelReservation(Problem):
    """Missing imagePullSecret fault on the Blueprint hotel-reservation app.

    Chosen over astronomy-shop for the same fault because it has the key property — a
    UNIQUE image per service, so gating/purging the target's image has zero blast radius —
    without the weight: 8 small Go service pods + light mongo/memcached (no Helm subcharts,
    no resource requests), so it fits on small clusters and on a local kind cluster with no
    capacity trimming. Blueprint deploys via plain `kubectl apply` (not Helm), so the stock
    app class is used directly here (no capacity-fitted subclass is needed).
    """

    def __init__(self):
        self.app = BlueprintHotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

        self.problem_id = "missing_image_pull_secret_blueprint_hotel_reservation"
        self.faulty_service = [_TARGET_DEPLOYMENT]

        self.secret_name = _SECRET_NAME
        self.target_deployment = _TARGET_DEPLOYMENT
        self.target_container = _TARGET_CONTAINER
        # Consumed by ImagePullSecretMitigationOracle (back-compat: defaults preserve the
        # hotel-reservation behaviour when these attributes are absent).
        self.target_pod_label = _TARGET_POD_LABEL
        self.gated_image_re = re.compile(rf":{_REGISTRY_PORT}/.*{re.escape(_PRIVATE_IMAGE_PATH)}")

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

    @mark_fault_injected
    def inject_fault(self) -> bool:
        logger.info("Injecting MissingImagePullSecret fault (blueprint-hotel-reservation / geo-service)...")

        # Step 1: start the private registry as an in-cluster Deployment+Service
        self._ensure_namespace(_INFRA_NAMESPACE)
        self._registry_ip = self._start_registry()
        self._private_image = f"{self._registry_ip}:{_REGISTRY_PORT}/{_PRIVATE_IMAGE_PATH}:latest"
        logger.info("Private registry running at %s", self._registry_ip)

        # Step 2: configure containerd on all nodes to allow HTTP pulls from this registry
        self._configure_containerd_insecure(self._registry_ip)

        # Step 3: re-pull the geo-service image from upstream and push it into the private
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

        # Step 7: purge the target's UNIQUE images (public AND private) from every node
        # BEFORE the docker.io block goes up.
        #  - Public purge closes the "repoint to public image + IfNotPresent" cheat (warm
        #    cache served the public image without re-pulling).
        #  - Private purge closes the subtler "just flip imagePullPolicy to IfNotPresent"
        #    cheat: the private image is cached from the step-5 baseline, so IfNotPresent
        #    would serve it WITHOUT the secret. Purging it forces a re-pull from the private
        #    registry, which still requires the secret — so only the real fix works. The
        #    legit fix re-pulls fine because the in-cluster registry is never blocked.
        # Both repos are unique to geo-service, so purging affects no sibling service.
        # Must run while docker.io is still open: _run_on_node uses busybox:1.36 (docker.io,
        # IfNotPresent); if the block were already up and busybox uncached on a node, the
        # node-op pod would ImagePullBackOff and the purge would silently fail.
        self._purge_image_from_nodes(self._source_image)
        self._purge_image_from_nodes(self._private_image)
        logger.info(
            "Purged unique public (%s) and private (%s) images from node caches",
            self._source_image,
            self._private_image,
        )

        # Step 8: block public-registry pulls (docker.io) so an agent can't bypass the
        # secret by repointing the image back to its public ref.
        # First wait for the whole app to be Ready so every 777lefty/* image is pulled and
        # cached BEFORE the node-wide docker.io block — otherwise a service still pulling its
        # image when the block goes up (e.g. profile-service, search-service) gets stuck in
        # ImagePullBackOff as collateral unrelated to the geo-service fault.
        self._wait_for_app_pods_ready()
        block_ip = self._start_dockerhub_block()
        self._configure_containerd_dockerhub_block(block_ip)
        logger.info("Public Docker Hub pulls are not allowed")

        # Step 9: inject the fault — delete the imagePullSecret
        try:
            self.core_v1.delete_namespaced_secret(name=_SECRET_NAME, namespace=self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        logger.info("Deleted imagePullSecret '%s'", _SECRET_NAME)

        # Step 10: force a pod restart so the missing secret is immediately observable
        # (with imagePullPolicy: Always, the pod only contacts the registry at start/restart)
        self._delete_deployment_pods()

        # Step 11: create decoy pods — emit warning but stay Running
        self._decoy_pod_names = self._create_decoy_pods()
        logger.info("Created %d decoy pods", len(self._decoy_pod_names))

        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        logger.info("Recovering from MissingImagePullSecret fault (blueprint-hotel-reservation / geo-service)...")

        # Step 1: recreate the imagePullSecret so the pod can pull again
        self._create_pull_secret()

        # Step 2: force a pod restart to trigger re-pull with the restored secret
        self._delete_deployment_pods()
        self._wait_for_deployment_ready(timeout=120)
        logger.info("Target pod Running with restored imagePullSecret")

        # Step 3: tear down the public-registry block before reverting the image, so public
        # pulls are unblocked again for the restored spec.
        self._remove_containerd_dockerhub_block()
        self._stop_dockerhub_block()

        # Step 4: revert Deployment to original image + imagePullPolicy + no secret reference
        if self._original_image and self._original_pull_policy:
            self._update_deployment(
                image=self._original_image,
                pull_policy=self._original_pull_policy,
                add_pull_secret=False,
            )
        # Wait for the rolling update to the original spec to complete
        self._wait_for_deployment_ready(timeout=120)
        logger.info("Deployment restored to original spec")

        # Step 5: delete decoy pods
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

        # Step 6: delete the imagePullSecret (no longer needed after image revert)
        try:
            self.core_v1.delete_namespaced_secret(name=_SECRET_NAME, namespace=self.namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

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

    @classmethod
    def _api_call(cls, fn, *, what: str, attempts: int = 6, base_delay: float = 2.0):
        """Run a Kubernetes API call, retrying transient control-plane failures.

        On a degraded / restarting control plane the apiserver intermittently returns a
        transient 5xx (notably 504 "request did not complete within the allotted timeout")
        or drops the connection mid-request (TLS handshake timeout, connection reset).
        These are not the caller's fault and usually clear within seconds — retry with
        backoff, waiting for the apiserver to answer again between attempts, instead of
        aborting the whole injection. Non-transient ApiExceptions (404, 409, 403, ...) are
        re-raised immediately so each caller's own create-or-replace handling still applies.
        """
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except client.exceptions.ApiException as e:
                if e.status not in _TRANSIENT_API_STATUSES:
                    raise
                last_exc = e
                logger.warning("Transient API error on %s: HTTP %s (attempt %d/%d)", what, e.status, attempt, attempts)
            except (urllib3.exceptions.HTTPError, OSError) as e:
                # Connection-level failures (TLS handshake timeout, connection reset, etc.)
                last_exc = e
                logger.warning("Transient API connection error on %s (attempt %d/%d): %s", what, attempt, attempts, e)
            if attempt < attempts:
                cls._wait_for_apiserver_ready(timeout=60)
                time.sleep(base_delay * attempt)
        assert last_exc is not None
        raise last_exc

    @classmethod
    def _ensure_namespace(cls, name: str) -> None:
        try:
            cls._api_call(
                lambda: client.CoreV1Api().create_namespace(
                    body=client.V1Namespace(metadata=client.V1ObjectMeta(name=name))
                ),
                what=f"create namespace {name}",
            )
        except client.exceptions.ApiException as e:
            if e.status != 409:
                raise

    @classmethod
    def _ensure_secret(cls, secret: client.V1Secret) -> None:
        core_v1 = client.CoreV1Api()
        ns, name = secret.metadata.namespace, secret.metadata.name
        try:
            cls._api_call(
                lambda: core_v1.create_namespaced_secret(namespace=ns, body=secret), what=f"create secret {name}"
            )
        except client.exceptions.ApiException as e:
            if e.status == 409:
                cls._api_call(
                    lambda: core_v1.replace_namespaced_secret(name=name, namespace=ns, body=secret),
                    what=f"replace secret {name}",
                )
            else:
                raise

    @classmethod
    def _ensure_config_map(cls, cm: client.V1ConfigMap) -> None:
        core_v1 = client.CoreV1Api()
        ns, name = cm.metadata.namespace, cm.metadata.name
        try:
            cls._api_call(
                lambda: core_v1.create_namespaced_config_map(namespace=ns, body=cm), what=f"create configmap {name}"
            )
        except client.exceptions.ApiException as e:
            if e.status == 409:
                cls._api_call(
                    lambda: core_v1.replace_namespaced_config_map(name=name, namespace=ns, body=cm),
                    what=f"replace configmap {name}",
                )
            else:
                raise

    @classmethod
    def _ensure_deployment(cls, deployment: client.V1Deployment) -> None:
        apps_v1 = client.AppsV1Api()
        ns, name = deployment.metadata.namespace, deployment.metadata.name
        try:
            cls._api_call(
                lambda: apps_v1.create_namespaced_deployment(namespace=ns, body=deployment),
                what=f"create deployment {name}",
            )
        except client.exceptions.ApiException as e:
            if e.status == 409:
                cls._api_call(
                    lambda: apps_v1.replace_namespaced_deployment(name=name, namespace=ns, body=deployment),
                    what=f"replace deployment {name}",
                )
            else:
                raise

    @classmethod
    def _ensure_service(cls, service: client.V1Service) -> str:
        """Create the Service if missing; return its ClusterIP either way."""
        core_v1 = client.CoreV1Api()
        ns, name = service.metadata.namespace, service.metadata.name
        try:
            created = cls._api_call(
                lambda: core_v1.create_namespaced_service(namespace=ns, body=service), what=f"create service {name}"
            )
            return created.spec.cluster_ip
        except client.exceptions.ApiException as e:
            if e.status == 409:
                return cls._api_call(
                    lambda: core_v1.read_namespaced_service(name=name, namespace=ns), what=f"read service {name}"
                ).spec.cluster_ip
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
        """Return "docker" for cri-dockerd nodes, "containerd" otherwise."""
        node = client.CoreV1Api().read_node(name=node_name)
        runtime_version = (node.status.node_info.container_runtime_version or "") if node.status else ""
        return "docker" if runtime_version.startswith("docker://") else "containerd"

    @staticmethod
    def _run_on_node(node_name: str, script: str, timeout: int = 60) -> str:
        """Run a shell `script` on `node_name` via a one-shot privileged pod.

        Modeled on `kubectl.py:_run_localpv_gc_pod_on_node`: busybox image,
        privileged, host root mounted at /host, tolerates all taints so it can
        land on control-plane nodes too. Runs with hostPID so `nsenter -t 1 ...`
        can reach the host's systemd / CRI.
        """
        core_v1 = client.CoreV1Api()
        short_node = node_name.split(".")[0].lower().replace("_", "-")[:40]
        pod_name = f"node-maintenance-{short_node}-{int(time.time()) % 100000}"[:63]
        namespace = "kube-system"

        pod_body = client.V1Pod(
            metadata=client.V1ObjectMeta(name=pod_name, namespace=namespace, labels={"app": "node-maintenance"}),
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
                # The script may restart containerd/docker on the control-plane node,
                # briefly knocking out the API server. Tolerate transient read failures
                # and keep polling rather than aborting the whole operation.
                try:
                    pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
                    phase = (pod.status.phase or "Pending") if pod.status else "Pending"
                except Exception as e:  # noqa: BLE001 - API server may be transiently unreachable
                    logger.debug("Transient error polling node-op pod %s (will retry): %s", pod_name, e)
                    phase = "Pending"
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
            with contextlib.suppress(Exception):
                core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace, grace_period_seconds=0)

    @staticmethod
    def _wait_for_apiserver_ready(timeout: int = 180) -> bool:
        """Block until the Kubernetes API server answers again.

        Restarting containerd/docker on a control-plane node briefly disrupts the API
        server (and the kubelet that serves it). Returning before it recovers makes the
        very next client/kubectl call fail with 'connection refused' — which previously
        cascaded all the way into unrelated conductor cleanup. Poll a cheap read until it
        succeeds (or give up after `timeout`, logging a warning rather than raising, so a
        genuinely-dead apiserver still surfaces at the real call site)."""
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                client.CoreV1Api().list_namespace(limit=1, _request_timeout=5)
                return True
            except Exception as e:  # noqa: BLE001 - ApiException, urllib3 conn errors, etc.
                last_err = e
                time.sleep(3)
        logger.warning("API server did not become ready within %ds (last error: %s)", timeout, last_err)
        return False

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
                                        client.V1EnvVar(name="REGISTRY_AUTH_HTPASSWD_REALM", value="Private Registry"),
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
        MissingImagePullSecretBlueprintHotelReservation._delete_deployment_and_service(
            _REGISTRY_DEPLOYMENT, _INFRA_NAMESPACE
        )
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
                    # Restarting containerd on a control-plane node briefly disrupts the
                    # API server. Wait for it to answer again BEFORE the next iteration,
                    # whose `_get_node_runtime` / `_run_on_node` calls would otherwise hit
                    # 'connection refused' and abort the whole injection.
                    self._wait_for_apiserver_ready()
            elif self._get_node_runtime(node) == "docker":
                # cri-dockerd node: dockerd reads /etc/docker/daemon.json, not
                # containerd's certs.d.
                script = self._daemon_json_insecure_registry_script(addr)
                if "RELOADED" in self._run_on_node(node, script):
                    restarted.append(node)
                    self._wait_for_apiserver_ready()
            else:
                script = (
                    f"mkdir -p /host{certs_dir} && printf '%s' {shlex.quote(hosts_toml)} > /host{certs_dir}/hosts.toml && "
                    f"if ! grep -q config_path /host/etc/containerd/config.toml; then "
                    f"printf '%s' {shlex.quote(config_path_snippet)} >> /host/etc/containerd/config.toml && "
                    f"nsenter -t 1 -m -u -n -i -- systemctl restart containerd && echo RESTARTED; fi"
                )
                if "RESTARTED" in self._run_on_node(node, script):
                    restarted.append(node)
                    self._wait_for_apiserver_ready()
        if restarted:
            # Give containerd a moment to finish reloading certs.d, then re-confirm the
            # API server is healthy before returning to the injection flow.
            logger.info("Restarted container runtime on %d node(s); confirming API server health...", len(restarted))
            time.sleep(5)
            self._wait_for_apiserver_ready()

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
            f"{MissingImagePullSecretBlueprintHotelReservation._DOCKER_RELOAD_CMD} echo RELOADED; fi"
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
            f"{MissingImagePullSecretBlueprintHotelReservation._DOCKER_RELOAD_CMD} echo RELOADED; fi"
        )

    def _configure_containerd_dockerhub_block(self, block_ip: str) -> None:
        """Redirect docker.io pulls to the 403-responder (containerd mirror on containerd
        nodes; /etc/hosts redirect on cri-dockerd nodes)."""
        addr = f"{block_ip}:{_DOCKERHUB_BLOCK_PORT}"
        hosts_toml = f'server = "http://{addr}"\n\n[host."http://{addr}"]\n  capabilities = ["pull", "resolve"]\n'
        for node in self._get_cluster_nodes():
            if node.startswith("kind-"):
                for host in _BLOCK_REGISTRY_HOSTS:
                    certs_dir = f"/etc/containerd/certs.d/{host}"
                    subprocess.run(["docker", "exec", node, "mkdir", "-p", certs_dir], check=True)
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
                        f.write(hosts_toml)
                        tmp = f.name
                    subprocess.run(["docker", "cp", tmp, f"{node}:{certs_dir}/hosts.toml"], check=True)
                    os.unlink(tmp)
            elif self._get_node_runtime(node) == "docker":
                # Point the blocked registry hostnames at the block Service via /etc/hosts.
                hosts_lines = "\n".join(
                    f"{block_ip} {h} {_DOCKERHUB_BLOCK_HOSTS_MARKER}" for h in _DOCKERHUB_BLOCK_HOSTS
                )
                script = f"printf '%s\\n' {shlex.quote(hosts_lines)} >> /host/etc/hosts"
                self._run_on_node(node, script)
            else:
                script_parts = []
                for host in _BLOCK_REGISTRY_HOSTS:
                    certs_dir = f"/etc/containerd/certs.d/{host}"
                    script_parts.append(
                        f"mkdir -p /host{certs_dir} && printf '%s' {shlex.quote(hosts_toml)} > /host{certs_dir}/hosts.toml"
                    )
                self._run_on_node(node, " && ".join(script_parts))

    @staticmethod
    def _remove_containerd_dockerhub_block() -> None:
        cls = MissingImagePullSecretBlueprintHotelReservation
        for node in cls._get_cluster_nodes():
            if node.startswith("kind-"):
                for host in _BLOCK_REGISTRY_HOSTS:
                    subprocess.run(
                        ["docker", "exec", node, "rm", "-rf", f"/etc/containerd/certs.d/{host}"],
                        capture_output=True,
                    )
            elif cls._get_node_runtime(node) == "docker":
                with contextlib.suppress(Exception):
                    script = f"sed -i '/{_DOCKERHUB_BLOCK_HOSTS_MARKER.replace('/', r'\\/')}/d' /host/etc/hosts"
                    cls._run_on_node(node, script)
            else:
                with contextlib.suppress(Exception):
                    rm_parts = [f"rm -rf /host/etc/containerd/certs.d/{host}" for host in _BLOCK_REGISTRY_HOSTS]
                    cls._run_on_node(node, " && ".join(rm_parts))

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
        MissingImagePullSecretBlueprintHotelReservation._delete_deployment_and_service(
            _DOCKERHUB_BLOCK_DEPLOYMENT, _INFRA_NAMESPACE
        )
        with contextlib.suppress(client.exceptions.ApiException):
            client.CoreV1Api().delete_namespaced_config_map(name=_DOCKERHUB_BLOCK_CONFIGMAP, namespace=_INFRA_NAMESPACE)

    # ── Targeted image purge (safe: the target repo is unique to `geo-service`) ────

    def _purge_image_from_nodes(self, image: str | None) -> None:
        """Evict `image` from every node's image store. Because each Blueprint service has
        a UNIQUE repository (`777lefty/docker-<svc>-container`), removing the target's
        public image affects no other service (the `:latest` tag is shared as a string, but
        the repo differs). This is what makes the "repoint to public image + IfNotPresent"
        cheat fail: the warm cache copy is gone, so the kubelet must re-pull — which the
        registry block denies."""
        if not image:
            return
        for node in self._get_cluster_nodes():
            if node.startswith("kind-"):
                subprocess.run(
                    ["docker", "exec", node, "crictl", "rmi", image],
                    capture_output=True,
                )
            elif self._get_node_runtime(node) == "docker":
                with contextlib.suppress(Exception):
                    self._run_on_node(node, f"nsenter -t 1 -m -u -n -i -- docker rmi -f {shlex.quote(image)} || true")
            else:
                with contextlib.suppress(Exception):
                    self._run_on_node(node, f"nsenter -t 1 -m -u -n -i -- crictl rmi {shlex.quote(image)} || true")

    # ── Crash-safe cleanup (no saved instance state) ──────────────────────────

    @staticmethod
    def _cleanup_duplicate_target_replicasets() -> None:
        """A prior run's rolling update (image repoint <-> revert) can leave an old
        ReplicaSet for `_TARGET_DEPLOYMENT` with `replicas > 0`; scale any others
        besides the primary down to 0."""
        apps_v1 = client.AppsV1Api()
        rs_list = apps_v1.list_namespaced_replica_set(
            namespace=_APP_NAMESPACE, label_selector=f"{_TARGET_POD_LABEL}={_TARGET_DEPLOYMENT}"
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
    def _ensure_kube_config() -> None:
        """Idempotently load a Kubernetes client config (in-cluster, else kubeconfig).

        Safe to call repeatedly; a no-op once a config is already active. Needed by the
        static crash-recovery path, which can run before any instance has loaded config.
        """
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

    @staticmethod
    def cleanup_leftovers() -> None:
        """Remove ALL MissingImagePullSecret state left behind by an interrupted run
        (e.g. Ctrl+C / crash before recover_fault).

        Runs in a fresh process with no saved instance state, so everything is
        reconstructed from cluster signals (the private-registry image marker, pod
        labels, and `certs.d/*:5000` path globs) rather than from `self._original_image`
        / `self._registry_ip` etc. This makes it a true superset of `recover_fault()`'s
        teardown.
        """
        cls = MissingImagePullSecretBlueprintHotelReservation
        # 0. Ensure a Kubernetes client config is loaded. cleanup_leftovers() is the
        #    crash-recovery entrypoint and may be invoked from a fresh process (e.g.
        #    `python -c ...`) where nothing has called load_kube_config() yet — without
        #    this the client falls back to localhost:80 and every call ConnectionRefuses.
        cls._ensure_kube_config()
        # 1. Remove the per-node registry block FIRST so public pulls are unblocked
        #    before the Deployment is restored to the public image below.
        cls._remove_containerd_dockerhub_block()
        # 2. Restore the target Deployment so its pods stop referencing the
        #    about-to-be-deleted private registry image.
        cls._restore_target_deployment()
        # 3. Decoy pods in the app namespace.
        cls._delete_decoy_pods()
        # 4. Runbook ConfigMap in the app namespace.
        cls._delete_runbook_configmap_static()
        # 5. Private-registry insecure config on each node.
        cls._remove_containerd_insecure_all()
        # 6. infra-registry namespace — registry, htpasswd, master secret, block
        #    ConfigMap all go with it. Individual deletes first, then the namespace.
        cls._stop_dockerhub_block()
        cls._stop_registry()
        cls._delete_infra_namespace_static()
        # 7. Stale ReplicaSets from prior rolling updates.
        cls._cleanup_duplicate_target_replicasets()

    @staticmethod
    def _restore_target_deployment() -> None:
        """Restore the target Deployment to its pre-fault image + imagePullPolicy and
        drop the injected imagePullSecret.

        The original image/policy are derived from a sibling service's image
        (`_canonical_target_image_and_policy`) — deliberately NOT from any on-cluster
        annotation, which would leak the fix to the agent. An active fault is detected
        purely from cluster signals: the target image points at the private registry,
        or the injected pull secret is still referenced."""
        apps_v1 = client.AppsV1Api()
        try:
            deploy = apps_v1.read_namespaced_deployment(name=_TARGET_DEPLOYMENT, namespace=_APP_NAMESPACE)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return
            raise

        pull_secrets = deploy.spec.template.spec.image_pull_secrets or []
        containers = deploy.spec.template.spec.containers or []
        target = next((c for c in containers if c.name == _TARGET_CONTAINER), containers[0] if containers else None)
        if target is None:
            return

        points_at_private = bool(target.image) and f":{_REGISTRY_PORT}/" in target.image
        has_our_secret = any(s.name == _SECRET_NAME for s in pull_secrets)
        if not points_at_private and not has_our_secret:
            return  # nothing we injected is present

        original_image, original_policy = (
            MissingImagePullSecretBlueprintHotelReservation._canonical_target_image_and_policy()
        )
        if original_image is None and points_at_private:
            logger.warning(
                "Deployment '%s' points at the private registry (%s) but no original image "
                "could be derived from a sibling service; leaving the image unchanged — "
                "manual restore may be required.",
                _TARGET_DEPLOYMENT,
                target.image,
            )

        if original_image:
            target.image = original_image
        target.image_pull_policy = original_policy
        deploy.spec.template.spec.image_pull_secrets = []

        with contextlib.suppress(client.exceptions.ApiException):
            apps_v1.replace_namespaced_deployment(name=_TARGET_DEPLOYMENT, namespace=_APP_NAMESPACE, body=deploy)
            logger.info("Restored Deployment '%s' to pre-fault image/pullPolicy", _TARGET_DEPLOYMENT)

    @staticmethod
    def _canonical_target_image_and_policy() -> tuple[str | None, str]:
        """Derive the target container's pre-fault image + imagePullPolicy WITHOUT reading
        any fault-revealing annotation. Returns (image_or_None, policy); policy defaults
        to "IfNotPresent".

        Every Blueprint app service renders `777lefty/docker-<deployment>-container:<tag>`
        (e.g. `777lefty/docker-user-service-container:latest`). Uniqueness is in the
        repository name, not the tag (which is uniformly `:latest`), so a sibling's image
        yields the target's by swapping `docker-<sibling>-container` for
        `docker-geo-service-container`. This is cluster-based (always available) and is not
        a fault fingerprint — the agent can already see every Deployment's image."""
        apps_v1 = client.AppsV1Api()
        target_token = f"docker-{_TARGET_DEPLOYMENT}-container"
        for peer in _PEER_APP_SERVICES:
            try:
                dep = apps_v1.read_namespaced_deployment(name=peer, namespace=_APP_NAMESPACE)
            except client.exceptions.ApiException:
                continue
            conts = (dep.spec.template.spec.containers or []) if dep.spec else []
            c = next((x for x in conts if x.name == f"{peer}-container"), conts[0] if conts else None)
            if not c or not c.image or f":{_REGISTRY_PORT}/" in c.image:
                continue
            peer_token = f"docker-{peer}-container"
            if peer_token in c.image:
                target_image = c.image.replace(peer_token, target_token)
                return target_image, (c.image_pull_policy or "IfNotPresent")
        logger.warning("Could not derive canonical target image from any sibling Blueprint service")
        return None, "IfNotPresent"

    @staticmethod
    def _delete_decoy_pods() -> None:
        """Delete decoy pods in the app namespace by their label (crash-safe)."""
        core_v1 = client.CoreV1Api()
        with contextlib.suppress(client.exceptions.ApiException):
            pods = core_v1.list_namespaced_pod(namespace=_APP_NAMESPACE, label_selector="custom-logger=true")
            for pod in pods.items:
                with contextlib.suppress(client.exceptions.ApiException):
                    core_v1.delete_namespaced_pod(
                        name=pod.metadata.name, namespace=_APP_NAMESPACE, grace_period_seconds=0
                    )

    @staticmethod
    def _delete_runbook_configmap_static() -> None:
        with contextlib.suppress(client.exceptions.ApiException):
            client.CoreV1Api().delete_namespaced_config_map(name=_RUNBOOK_CONFIGMAP, namespace=_APP_NAMESPACE)

    @staticmethod
    def _delete_infra_namespace_static() -> None:
        with contextlib.suppress(client.exceptions.ApiException):
            client.CoreV1Api().delete_namespace(name=_INFRA_NAMESPACE)

    @staticmethod
    def _remove_containerd_insecure_all() -> None:
        """Addr-agnostic removal of the private-registry insecure config left on nodes.
        cleanup_leftovers has no saved registry IP, so we key off the fixed registry port
        (`*:5000`) for containerd certs.d, and off the presence of an `insecure-registries`
        entry for cri-dockerd daemon.json."""
        cls = MissingImagePullSecretBlueprintHotelReservation
        port = _REGISTRY_PORT
        for node in cls._get_cluster_nodes():
            if node.startswith("kind-"):
                subprocess.run(
                    ["docker", "exec", node, "sh", "-c", f"rm -rf /etc/containerd/certs.d/*:{port}"],
                    capture_output=True,
                )
            elif cls._get_node_runtime(node) == "docker":
                with contextlib.suppress(Exception):
                    cls._run_on_node(node, cls._daemon_json_remove_insecure_registry_script(""))
            else:
                with contextlib.suppress(Exception):
                    cls._run_on_node(node, f"rm -rf /host/etc/containerd/certs.d/*:{port}")

    def _create_infra_namespace_and_secret(self) -> None:
        """Create the 'platform team' namespace holding the source-of-truth registry
        credentials, stored as raw fields (not a pre-built dockerconfigjson) so the
        agent must still construct a correct imagePullSecret itself."""
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
        """Push the upstream image to the in-cluster private registry via
        `kubectl port-forward` (works on kind and real clusters alike, as it only needs
        API server access).

        The image is pulled only if it is not already in the local Docker cache, and the
        temporary push tag is removed afterwards. The upstream image is kept in the local
        Docker cache so subsequent runs skip the (large) re-pull."""
        already_cached = (
            subprocess.run(
                ["docker", "image", "inspect", upstream_ref],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if already_cached:
            logger.info("Image %s already in local Docker cache; skipping pull", upstream_ref)
        else:
            logger.info("Image is not in local Docker cache; pulling")
            subprocess.run(["docker", "pull", upstream_ref], check=True)

        local_port = _LOCAL_PORT_FORWARD_PORT
        host_tag = f"localhost:{local_port}/{_PRIVATE_IMAGE_PATH}:latest"
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
            for _ in range(60):
                if port_forward.poll() is not None:
                    raise RuntimeError(f"kubectl port-forward exited early with code {port_forward.returncode}")
                with contextlib.suppress(OSError), socket.create_connection(("localhost", local_port), timeout=1):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(f"kubectl port-forward did not become ready on localhost:{local_port}")

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
            subprocess.run(
                ["docker", "rmi", host_tag],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

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

            for c in containers:
                if target is not None and c.name == target.name:
                    c.image = image
                    c.image_pull_policy = pull_policy
                    break

            deploy.spec.template.spec.image_pull_secrets = (
                [client.V1LocalObjectReference(name=_SECRET_NAME)] if add_pull_secret else []
            )

            try:  # avoid races between our read+PUT and the rollout controller's update
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
            label_selector=f"{_TARGET_POD_LABEL}={_TARGET_DEPLOYMENT}",
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

    @staticmethod
    def _pod_is_ready(pod) -> bool:
        """A pod counts as ready if it Succeeded (one-shot job) or all its containers
        report ready."""
        phase = (pod.status.phase if pod.status else None) or ""
        if phase == "Succeeded":
            return True
        css = pod.status.container_statuses if pod.status else None
        return bool(css) and all(cs.ready for cs in css)

    def _wait_for_app_pods_ready(self, timeout: int = 300, sleep_s: int = 5) -> None:
        """Wait until every pod in the app namespace is Ready before the docker.io block.

        The block redirects docker.io node-wide, so any 777lefty/* service image still being
        pulled when it goes up gets stuck in ImagePullBackOff — collateral on non-target
        services (e.g. profile-service, search-service) that has nothing to do with the
        injected geo-service fault. Gating on full app readiness ensures every service image
        is pulled and cached first, so once the block is up only the deliberately-purged
        geo-service image can fail. Non-fatal on timeout: a pre-existing unhealthy app is not
        something inject should abort on, but we log loudly so the collateral is explainable."""
        waited = 0
        while waited < timeout:
            try:
                pods = self.core_v1.list_namespaced_pod(namespace=self.namespace).items or []
                if pods and all(self._pod_is_ready(p) for p in pods):
                    logger.info(
                        "All %d pods in '%s' are Ready; safe to raise docker.io block",
                        len(pods),
                        self.namespace,
                    )
                    return
            except client.exceptions.ApiException as e:
                logger.warning("Transient error polling app pod readiness (will retry): %s", e)
            time.sleep(sleep_s)
            waited += sleep_s
        logger.warning(
            "Not all pods in '%s' became Ready within %ds; raising docker.io block anyway "
            "(non-target services with un-cached docker.io images may ImagePullBackOff)",
            self.namespace,
            timeout,
        )

    def _create_decoy_pods(self) -> list[str]:
        """Create bare pods that emit FailedToRetrieveImagePullSecret but stay Running.

        Each decoy references a non-existent imagePullSecret (the warning source) but runs
        the cluster's pause image so the container reliably stays Running+Ready as a
        distractor. We deliberately do NOT reuse a sibling Blueprint service image: those
        777lefty/* images are minimal Go containers (no `sleep`/shell, and their service
        entrypoint crashes without its dependencies), so the decoy would CrashLoopBackOff
        and trip the namespace-wide `wait_for_pods_ready` check. The pause image is cached
        on every node (every pod sandbox uses it), runs forever by design, is tiny, and
        lives on registry.k8s.io — which is NOT blocked by this fault (only docker.io is).
        IfNotPresent serves it from the warm node cache. The FailedToRetrieveImagePullSecret
        warning fires from the missing secret reference regardless of the container command."""
        names = []
        for i in range(2):
            name = f"blueprint-logger-{i}"
            spec = client.V1PodSpec(
                image_pull_secrets=[client.V1LocalObjectReference(name=_DECOY_SECRET)],
                restart_policy="Always",
                tolerations=[client.V1Toleration(operator="Exists")],
                containers=[
                    client.V1Container(
                        name="custom-logger",
                        image=_DECOY_IMAGE,
                        image_pull_policy="IfNotPresent",
                    )
                ],
            )
            pod = client.V1Pod(
                metadata=client.V1ObjectMeta(
                    name=name,
                    namespace=self.namespace,
                    labels={"custom-logger": "true"},
                ),
                spec=spec,
            )
            try:
                self.core_v1.create_namespaced_pod(namespace=self.namespace, body=pod)
            except client.exceptions.ApiException as e:
                if e.status != 409:
                    raise
            names.append(name)
        return names
