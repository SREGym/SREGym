"""Cumulative admission-webhook timeout on Hotel Reservation.

This problem reproduces a known Kubernetes admission-controller trap: when a
chain of admission webhooks individually have ``failurePolicy: Ignore``, an
operator typically assumes they are safe by default. They are not, in
aggregate. The kube-apiserver enforces a global admission deadline (~30
seconds in the upstream defaults), and the per-webhook ``Ignore`` policy
only applies after each webhook's own ``timeoutSeconds`` elapses. With
enough webhooks whose backends are unreachable, the cumulative waiting time
exceeds the global deadline before any individual ``Ignore`` can fire. The
kube-apiserver returns ``context deadline exceeded`` and the error names no
offending webhook.

The fault here adds four mutating admission webhooks scoped to the Hotel
Reservation namespace, each with ``failurePolicy=Ignore`` and an uneven
per-webhook ``timeoutSeconds`` (12s, 8s, 11s, 9s). A default-deny ingress
NetworkPolicy in the webhooks' namespace blocks all kube-apiserver ->
backend connections, so every call hangs to its full per-webhook
timeout. The cumulative 40s of waiting overshoots the 30s global
deadline; the global timeout fires first; pod admission fails.

Application impact is direct. The injection deletes the running
``recommendation`` pod. The ReplicaSet attempts to recreate it; admission
fails; the deployment loses its only replica; the ``recommendation``
Service has no endpoints; ``frontend`` calls to ``recommendation``
degrade.

Citations:

- `kubernetes/kubernetes#128162 <https://github.com/kubernetes/kubernetes/issues/128162>`_
  (October 2024, closed as not planned): documents the exact recipe and
  the diagnostic difficulty (error message names no offending webhook).
  The trap reproduces on the 1.32.0 cluster this problem targets.
"""

import base64
import datetime
import logging
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.cumulative_admission_webhook_timeout_mitigation import (
    CumulativeAdmissionWebhookTimeoutMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class CumulativeAdmissionWebhookTimeoutHotelReservation(Problem):
    """Four mutating webhooks whose cumulative timeout starves the
    kube-apiserver's global admission deadline.

    Each webhook has ``failurePolicy=Ignore`` and is scoped to the Hotel
    Reservation namespace via ``namespaceSelector``. Per-webhook
    ``timeoutSeconds`` are uneven (12s, 8s, 11s, 9s); the values are
    individually unremarkable but their sum, 40s, sits above the
    apiserver's global admission deadline. A default-deny ingress
    ``NetworkPolicy`` in the webhooks' namespace makes every backend
    call hang to its full timeout. The per-webhook ``Ignore`` only
    triggers after that webhook's own ``timeoutSeconds`` elapses, and
    the global ~30s admission deadline fires before the cumulative 40s
    of waiting can complete. The kube-apiserver returns ``context
    deadline exceeded`` with no offending webhook named in the error.

    The accepted fix space is wide. Any of the following recover the
    cluster:

    - Open the default-deny NetworkPolicy to allow ingress from the
      kube-apiserver source (or delete the NetworkPolicy entirely).
    - Lower one or more webhooks' ``timeoutSeconds`` so the cumulative
      total comfortably fits below the global ~30s deadline.
    - Narrow at least one webhook's ``namespaceSelector`` to exclude the
      Hotel Reservation namespace.
    - Delete one or more (but not all) of the four webhook
      configurations.

    Rejected shortcuts:

    - Delete all four webhooks: removes the compliance / policy plane
      entirely. The oracle rejects this with a "policy plane must
      remain present" message.
    - Delete the ``recommendation`` deployment, scale it to zero, or
      delete the application namespace: destroys the workload rather
      than fixing the admission path.
    """

    # ------------------------------------------------------------------
    # Target component (the application side of the fault)
    # ------------------------------------------------------------------
    TARGET_DEPLOYMENT = "recommendation"

    # ------------------------------------------------------------------
    # Webhook plane (the cluster-control side of the fault)
    # ------------------------------------------------------------------
    POLICY_NAMESPACE = "policy-system"
    NETWORK_POLICY_NAME = "default-deny-ingress"

    # Realistic-sounding names so the agent reads them as legitimate
    # compliance controls rather than as benchmark artifacts.
    WEBHOOK_BACKEND_NAMES = (
        "pod-resource-validator",
        "audit-log-enforcer",
        "image-policy-checker",
        "tenant-quota-validator",
    )
    WEBHOOK_BACKEND_IMAGE = "python:3.12-alpine"
    WEBHOOK_BACKEND_PORT = 443

    # Names of the cluster-scoped Secret and ConfigMap that every backend
    # mounts. The Secret holds the TLS server cert and key, signed by the
    # CA that the webhook configs reference in caBundle. The ConfigMap
    # holds the Python admission-webhook server source that each backend
    # runs.
    TLS_SECRET_NAME = "compliance-webhook-tls"
    SCRIPT_CONFIGMAP_NAME = "compliance-webhook-server"

    # The webhook server source. Each backend Deployment runs this script
    # via `python /app/server.py`. The script terminates TLS using the
    # cert and key mounted at /tls, reads incoming AdmissionReviews, and
    # decides allow/deny based on two policy checks: a trusted-image
    # registry allowlist and a per-container CPU request ceiling. The
    # allowlist and ceiling are loose enough that every Hotel
    # Reservation workload passes; the rules are real but the policy
    # plane is intentionally non-restrictive against the application
    # under test. Denials return a proper AdmissionReview with a 403
    # status and a human-readable message, matching how production
    # webhook servers (Kyverno, OPA Gatekeeper, etc.) format
    # rejections.
    #
    # Access logs are suppressed. During the fault window no requests
    # reach the container anyway (the NetworkPolicy drops them before
    # TLS), so logs would be silent regardless; keeping them off avoids
    # noise during normal operation.
    WEBHOOK_SERVER_SCRIPT = """\
import json
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

TRUSTED_IMAGE_PREFIXES = (
    # Trusted registries.
    "docker.io/",
    "gcr.io/",
    "ghcr.io/",
    "quay.io/",
    "registry.k8s.io/",
    "k8s.gcr.io/",
    # Common base images on Docker Hub's library namespace, which kubelet
    # renders without an explicit registry prefix.
    "busybox:",
    "alpine:",
    "debian:",
    "ubuntu:",
    "python:",
    "nginx:",
    "redis:",
    "mongo:",
    "memcached:",
    "consul:",
    # Common third-party tooling images.
    "jaegertracing/",
    "prometheus/",
    "grafana/",
    "envoyproxy/",
    # Internal application images.
    "yinfangchen/",
)
MAX_CPU_REQUEST_M = 4000


def _image_trusted(image):
    return any(image.startswith(p) for p in TRUSTED_IMAGE_PREFIXES)


def _cpu_millis(req):
    if not req:
        return 0
    if req.endswith("m"):
        try:
            return int(req[:-1])
        except ValueError:
            return 0
    try:
        return int(float(req) * 1000)
    except ValueError:
        return 0


def _evaluate(review):
    request = review.get("request", {})
    obj = request.get("object") or {}
    spec = obj.get("spec") or {}
    containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
    for c in containers:
        image = c.get("image", "")
        if image and not _image_trusted(image):
            return False, f"image {image!r} is not from a trusted registry"
        cpu = ((c.get("resources") or {}).get("requests") or {}).get("cpu", "")
        if _cpu_millis(cpu) > MAX_CPU_REQUEST_M:
            return (
                False,
                f"container {c.get('name', '?')!r} requests "
                f"{cpu} CPU, above the {MAX_CPU_REQUEST_M}m policy ceiling",
            )
    return True, ""


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            review = json.loads(self.rfile.read(length))
        except Exception:
            review = {}
        allowed, message = _evaluate(review)
        uid = review.get("request", {}).get("uid", "")
        resp = {"uid": uid, "allowed": allowed}
        if not allowed:
            resp["status"] = {"code": 403, "message": message}
        body = json.dumps(
            {
                "apiVersion": "admission.k8s.io/v1",
                "kind": "AdmissionReview",
                "response": resp,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
srv = HTTPServer(("0.0.0.0", 443), Handler)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
"""

    # Per-backend webhook timeoutSeconds. Values are uneven so the
    # cumulative cannot be computed by counting webhooks and multiplying
    # a single number. The sum (40s) sits above the kube-apiserver's
    # ~30s global admission deadline, which is what triggers the fault.
    WEBHOOK_TIMEOUTS_S = {
        "pod-resource-validator": 12,
        "audit-log-enforcer": 8,
        "image-policy-checker": 11,
        "tenant-quota-validator": 9,
    }

    # ------------------------------------------------------------------
    # Lifecycle timeouts
    # ------------------------------------------------------------------
    # Time to wait for the webhook backend deployments to become Available.
    BACKEND_ROLLOUT_TIMEOUT_S = 120

    # Time to wait for the application-impact symptom to manifest after
    # the recommendation pod is deleted (ReplicaSet recreation attempts +
    # admission timeouts each take ~30s; the margin here is generous).
    SYMPTOM_TIMEOUT_S = 180
    SYMPTOM_POLL_INTERVAL_S = 5

    # Time to wait for the recommendation deployment to converge after
    # recover_fault opens the NetworkPolicy.
    RECOVERY_TIMEOUT_S = 180
    RECOVERY_POLL_INTERVAL_S = 5

    # ------------------------------------------------------------------
    # Decoy webhooks
    # ------------------------------------------------------------------
    # Four decoys named after real production tools (cert-manager, Istio,
    # Kyverno, Linkerd). Each shares the same backend reference, caBundle,
    # failurePolicy, sideEffects, admissionReviewVersions, and
    # namespaceSelector (matching hotel-reservation) as the four real
    # webhooks, so an agent listing webhooks scoped to the application
    # namespace sees 8 hits, not 4.
    #
    # Each decoy is kept inert by either:
    #   - Rules targeting CRDs/APIs that are not installed (cert-manager)
    #   - objectSelector requiring an opt-in pod label no HR pod carries
    #     (istio, kyverno, linkerd)
    #
    # Decoys use a short timeoutSeconds=5 so they do not on their own
    # push cumulative timeout over the global admission deadline even if
    # one were ever invoked. The cumulative-timeout fault remains
    # entirely the responsibility of the four real webhooks.
    DECOY_WEBHOOKS = (
        {
            "name": "cert-manager-webhook",
            "webhook_name": "webhook.cert-manager.io",
            "rules": [
                {
                    "apiGroups": ["cert-manager.io"],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE", "UPDATE"],
                    "resources": ["certificates", "issuers"],
                    "scope": "*",
                }
            ],
            "object_selector": None,
        },
        {
            "name": "istio-sidecar-injector",
            "webhook_name": "rev.namespace.sidecar-injector.istio.io",
            "rules": [
                {
                    "apiGroups": [""],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE"],
                    "resources": ["pods"],
                    "scope": "Namespaced",
                }
            ],
            "object_selector": {"matchLabels": {"sidecar.istio.io/inject": "true"}},
        },
        {
            "name": "kyverno-resource-mutating-webhook-cfg",
            "webhook_name": "mutate.kyverno.svc",
            "rules": [
                {
                    "apiGroups": [""],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE", "UPDATE"],
                    "resources": ["pods"],
                    "scope": "Namespaced",
                }
            ],
            "object_selector": {"matchLabels": {"kyverno.io/managed": "enabled"}},
        },
        {
            "name": "linkerd-proxy-injector-webhook-config",
            "webhook_name": "linkerd-proxy-injector.linkerd.io",
            "rules": [
                {
                    "apiGroups": [""],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE"],
                    "resources": ["pods"],
                    "scope": "Namespaced",
                }
            ],
            "object_selector": {"matchLabels": {"linkerd.io/inject": "enabled"}},
        },
    )
    DECOY_WEBHOOK_TIMEOUT_SECONDS = 5

    def __init__(self):
        super().__init__(app=HotelReservation())
        self.kubectl = KubeCtl()
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.networking_v1 = client.NetworkingV1Api()
        self.admissionregistration_v1 = client.AdmissionregistrationV1Api()

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.TARGET_DEPLOYMENT}",
            namespace=self.namespace,
            description=(
                f"Pod creation in the `{self.namespace}` namespace fails with "
                "`Timeout: request did not complete within requested timeout - "
                "context deadline exceeded` because the kube-apiserver's global "
                "admission deadline (approximately 30 seconds) is exceeded by "
                "the cumulative waiting time across a chain of mutating "
                "admission webhooks. Several MutatingWebhookConfigurations "
                f"target this namespace via `namespaceSelector` matching "
                f"`{self.namespace}`, each with `failurePolicy: Ignore` and "
                "per-webhook `timeoutSeconds` whose sum across the chain is "
                f"approximately {sum(self.WEBHOOK_TIMEOUTS_S.values())} seconds. "
                "Their backend Services live in the "
                f"`{self.POLICY_NAMESPACE}` namespace, where a default-deny "
                f"`NetworkPolicy` named `{self.NETWORK_POLICY_NAME}` blocks "
                "all ingress; the kube-apiserver cannot reach the backends and "
                "every webhook call hangs until its individual timeoutSeconds "
                "expires. The cumulative waiting time exceeds the global "
                "admission deadline before the per-webhook `Ignore` policy "
                "can apply. The error message does not name the offending "
                "webhooks because the failure originates in the apiserver-side "
                "aggregate deadline rather than any one webhook call. The "
                "policy plane includes additional companion "
                "MutatingWebhookConfigurations (cert-manager, istio, kyverno, "
                "linkerd-style names) that share the same namespaceSelector "
                "but remain inert (rules target uninstalled CRDs, or "
                "objectSelectors require opt-in pod labels no application pod "
                "carries); these companions are not the fault and disabling "
                "them does not recover the cluster. Application impact: the "
                f"`{self.TARGET_DEPLOYMENT}` deployment's ReplicaSet cannot "
                "recreate its pod after disruption, the deployment shows "
                f"`0/1 ready`, the `{self.TARGET_DEPLOYMENT}` Service has no "
                "endpoints, and downstream calls from `frontend` degrade."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # Deploy Hotel Reservation up-front so the mitigation oracle has a
        # live application to probe (and so inject_fault has a recommendation
        # pod to delete).
        self.app.create_workload()

        self.mitigation_oracle = CumulativeAdmissionWebhookTimeoutMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        """Inject the cumulative admission-webhook timeout fault.

        Sequence:
            1. Create the policy namespace (idempotent).
            2. Generate a self-signed CA and a server cert signed by
               it whose SubjectAlternativeName covers every backend
               Service's DNS name. The CA goes into each webhook's
               ``caBundle``. The server cert and key go into a Secret
               that every backend mounts at ``/tls``. The Python
               webhook server script goes into a ConfigMap that every
               backend mounts at ``/app``.
            3. Create four backend Deployments + Services in the policy
               namespace (idempotent).
            4. Wait for backends to be Available.
            5. Apply the default-deny ingress NetworkPolicy (idempotent).
            6. Create four MutatingWebhookConfigurations scoped to the
               application namespace via ``namespaceSelector``, each
               with its own ``timeoutSeconds`` from ``WEBHOOK_TIMEOUTS_S``.
            7. Delete the running target deployment's pod so the
               ReplicaSet attempts to recreate it; admission fails because
               of the cumulative webhook timeout, and the new pod is
               never admitted.
            8. Poll until ``status.ready_replicas < status.replicas`` on
               the target deployment, confirming the application-impact
               symptom is live before returning.
        """
        logger.info("== Fault injection: cumulative admission-webhook timeout ==")

        # Step 1: policy namespace
        self._ensure_namespace(self.POLICY_NAMESPACE)

        # Step 2: generate the CA + server cert and persist the cert,
        # key, and server script the backends need at runtime.
        ca_bundle_b64, server_cert_pem, server_key_pem = self._generate_tls_material()
        self._ensure_tls_secret(server_cert_pem, server_key_pem)
        self._ensure_server_script_configmap()

        # Step 3 + 4: backend deployments + services
        for backend_name in self.WEBHOOK_BACKEND_NAMES:
            self._ensure_backend_deployment(backend_name)
            self._ensure_backend_service(backend_name)
        for backend_name in self.WEBHOOK_BACKEND_NAMES:
            self._wait_for_deployment_ready(
                name=backend_name,
                namespace=self.POLICY_NAMESPACE,
                timeout_s=self.BACKEND_ROLLOUT_TIMEOUT_S,
            )
        logger.info(f"All {len(self.WEBHOOK_BACKEND_NAMES)} backend deployments ready in '{self.POLICY_NAMESPACE}'.")

        # Step 5: default-deny ingress NetworkPolicy
        self._ensure_default_deny_network_policy()

        # Step 6: MutatingWebhookConfigurations (real ones)
        for backend_name in self.WEBHOOK_BACKEND_NAMES:
            self._ensure_mutating_webhook(backend_name, ca_bundle_b64)

        # Step 6b: decoy MutatingWebhookConfigurations
        self._install_decoy_webhooks(ca_bundle_b64)

        cumulative = sum(self.WEBHOOK_TIMEOUTS_S.values())
        logger.info(
            f"All {len(self.WEBHOOK_BACKEND_NAMES)} MutatingWebhookConfigurations "
            f"in place with failurePolicy=Ignore, scoped via namespaceSelector to "
            f"'{self.namespace}', cumulative timeoutSeconds={cumulative}s."
        )

        # Step 7: trigger the symptom by deleting the target's pod
        self._delete_target_pod()

        # Step 8: wait for ready_replicas < spec.replicas (symptom live)
        self._wait_for_symptom()
        logger.info(
            f"Fault is live: '{self.TARGET_DEPLOYMENT}' deployment in "
            f"'{self.namespace}' shows ready_replicas < spec.replicas. New pods "
            "cannot be admitted due to the cumulative webhook timeout exceeding "
            "the kube-apiserver's global admission deadline."
        )

    @mark_fault_injected
    def recover_fault(self):
        """Recover by removing the decoys and deleting the default-deny
        NetworkPolicy.

        Sequence:
            1. Remove the decoy MutatingWebhookConfigurations. These are
               purely SREGym setup artifacts and should not persist into
               post-recovery cluster state. Removing them before the
               cluster-state reconciler runs at teardown ensures clean
               separation of problem-specific cleanup from the broad
               'delete unexpected resources' sweep.
            2. Delete the default-deny NetworkPolicy in the policy
               namespace, restoring kube-apiserver -> webhook backend
               connectivity. The backend services still do not actually
               respond, so each per-webhook call still hits its
               ``timeoutSeconds``; but at that point the per-webhook
               ``failurePolicy: Ignore`` fires correctly within the
               global admission deadline.
            3. Wait for the target deployment to converge back to ready.

        The real webhooks, backends, and policy namespace stay in place;
        only the NetworkPolicy changes. This matches the operator fix:
        adjust the ingress rule, do not tear down the policy plane.
        """
        logger.info("== Fault recovery: remove decoys + open default-deny NetworkPolicy ==")

        # Step 1: remove decoys before any other recovery action.
        self._remove_decoy_webhooks()

        try:
            self.networking_v1.delete_namespaced_network_policy(
                name=self.NETWORK_POLICY_NAME,
                namespace=self.POLICY_NAMESPACE,
            )
            logger.info(f"Deleted NetworkPolicy '{self.NETWORK_POLICY_NAME}' in '{self.POLICY_NAMESPACE}'.")
        except ApiException as e:
            if e.status == 404:
                logger.info(f"NetworkPolicy '{self.NETWORK_POLICY_NAME}' already absent.")
            else:
                raise

        self._wait_for_recovery()
        logger.info(
            f"Recovery converged: '{self.TARGET_DEPLOYMENT}' deployment in "
            f"'{self.namespace}' is ready_replicas == spec.replicas."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_namespace(self, name: str) -> None:
        """Create the namespace if it doesn't already exist."""
        try:
            self.core_v1.create_namespace(body=client.V1Namespace(metadata=client.V1ObjectMeta(name=name)))
            logger.info(f"Created namespace '{name}'.")
        except ApiException as e:
            if e.status == 409:
                logger.info(f"Namespace '{name}' already exists.")
            else:
                raise

    def _generate_tls_material(self) -> tuple[str, str, str]:
        """Generate the TLS material that the webhook plane needs: a
        self-signed CA, plus a leaf server cert signed by that CA whose
        SubjectAlternativeName covers every backend Service's in-cluster
        DNS name. Returns ``(ca_bundle_b64, server_cert_pem,
        server_key_pem)``.

        The CA goes into each MutatingWebhookConfiguration's ``caBundle``.
        The server cert and key go into a Secret that every backend
        Deployment mounts at ``/tls``. The Python webhook server uses
        those files to terminate TLS on port 443. The kube-apiserver
        validates the server cert against the CA at handshake time, so
        post-recovery admission completes cleanly because every Hotel
        Reservation workload passes each backend's compliance check.

        Both certs expire after one day. Inject regenerates them on
        every run, so the short lifetime is fine for benchmark runs.
        """
        now = datetime.datetime.now(datetime.UTC)
        not_before = now - datetime.timedelta(minutes=5)
        not_after = now + datetime.timedelta(days=1)

        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "compliance-webhook-ca")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(ca_key, hashes.SHA256())
        )

        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sans = []
        for backend_name in self.WEBHOOK_BACKEND_NAMES:
            sans.append(x509.DNSName(f"{backend_name}.{self.POLICY_NAMESPACE}.svc"))
            sans.append(x509.DNSName(f"{backend_name}.{self.POLICY_NAMESPACE}.svc.cluster.local"))
        server_cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "compliance-webhook")]))
            .issuer_name(ca_name)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(ca_key, hashes.SHA256())
        )

        ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
        server_cert_pem = server_cert.public_bytes(serialization.Encoding.PEM).decode()
        server_key_pem = server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        return base64.b64encode(ca_pem).decode(), server_cert_pem, server_key_pem

    def _ensure_tls_secret(self, server_cert_pem: str, server_key_pem: str) -> None:
        """Create or update the Secret that holds the webhook server's
        TLS cert and key. Every backend Deployment mounts this Secret
        at ``/tls``. The cert has SANs covering every backend Service's
        DNS name, so a single Secret serves all four backends."""
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=self.TLS_SECRET_NAME, namespace=self.POLICY_NAMESPACE),
            type="kubernetes.io/tls",
            string_data={"tls.crt": server_cert_pem, "tls.key": server_key_pem},
        )
        try:
            self.core_v1.create_namespaced_secret(namespace=self.POLICY_NAMESPACE, body=body)
            logger.info(f"Created Secret '{self.TLS_SECRET_NAME}' in '{self.POLICY_NAMESPACE}'.")
        except ApiException as e:
            if e.status == 409:
                existing = self.core_v1.read_namespaced_secret(
                    name=self.TLS_SECRET_NAME, namespace=self.POLICY_NAMESPACE
                )
                body.metadata.resource_version = existing.metadata.resource_version
                self.core_v1.replace_namespaced_secret(
                    name=self.TLS_SECRET_NAME, namespace=self.POLICY_NAMESPACE, body=body
                )
                logger.info(f"Replaced Secret '{self.TLS_SECRET_NAME}' in '{self.POLICY_NAMESPACE}'.")
            else:
                raise

    def _ensure_server_script_configmap(self) -> None:
        """Create or update the ConfigMap that holds the Python webhook
        server source. Every backend Deployment mounts this ConfigMap
        at ``/app`` and runs ``python /app/server.py`` as its
        container command."""
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=self.SCRIPT_CONFIGMAP_NAME, namespace=self.POLICY_NAMESPACE),
            data={"server.py": self.WEBHOOK_SERVER_SCRIPT},
        )
        try:
            self.core_v1.create_namespaced_config_map(namespace=self.POLICY_NAMESPACE, body=body)
            logger.info(f"Created ConfigMap '{self.SCRIPT_CONFIGMAP_NAME}' in '{self.POLICY_NAMESPACE}'.")
        except ApiException as e:
            if e.status == 409:
                existing = self.core_v1.read_namespaced_config_map(
                    name=self.SCRIPT_CONFIGMAP_NAME, namespace=self.POLICY_NAMESPACE
                )
                body.metadata.resource_version = existing.metadata.resource_version
                self.core_v1.replace_namespaced_config_map(
                    name=self.SCRIPT_CONFIGMAP_NAME, namespace=self.POLICY_NAMESPACE, body=body
                )
                logger.info(f"Replaced ConfigMap '{self.SCRIPT_CONFIGMAP_NAME}' in '{self.POLICY_NAMESPACE}'.")
            else:
                raise

    def _ensure_backend_deployment(self, name: str) -> None:
        """Create a webhook backend Deployment. Each backend runs a
        small Python HTTPS server that terminates TLS on the webhook
        port, evaluates each incoming AdmissionReview against a
        trusted-image-registry allowlist and a per-container CPU
        ceiling, and returns either ``allowed: true`` or a 403 denial.
        The allowlist and ceiling are loose enough that every Hotel
        Reservation workload passes. The cert and key are mounted from
        the ``compliance-webhook-tls`` Secret at ``/tls``, and the
        server source is mounted from the ``compliance-webhook-server``
        ConfigMap at ``/app``. Both resources are set up earlier in
        inject_fault and shared by all four backends.

        Under normal conditions the backend works correctly. The fault
        is entirely in the network path: the default-deny NetworkPolicy
        in the policy namespace drops the kube-apiserver's SYN before
        it reaches the container, so during the fault window each
        webhook call waits the full per-webhook ``timeoutSeconds``
        before the apiserver gives up. After recovery, the apiserver
        completes TLS against the mounted cert, sends an
        AdmissionReview, and gets the allowed response back from each
        backend in turn, which is how the chain would behave in
        production once the network is opened.

        Backends run on worker nodes only, pinned via ``nodeAffinity``
        that excludes the control-plane label. The kube-apiserver runs
        with ``hostNetwork: true`` on the control-plane, and Calico
        does not enforce ingress NetworkPolicy on traffic from a
        host-network sender to a pod on the same node. Pinning the
        backends off the control-plane keeps every
        apiserver-to-backend connection cross-node, which is where the
        NetworkPolicy actually takes effect.
        """
        container = client.V1Container(
            name="app",
            image=self.WEBHOOK_BACKEND_IMAGE,
            command=["python", "/app/server.py"],
            ports=[client.V1ContainerPort(container_port=self.WEBHOOK_BACKEND_PORT)],
            volume_mounts=[
                client.V1VolumeMount(name="tls", mount_path="/tls", read_only=True),
                client.V1VolumeMount(name="app", mount_path="/app", read_only=True),
            ],
        )
        affinity = client.V1Affinity(
            node_affinity=client.V1NodeAffinity(
                required_during_scheduling_ignored_during_execution=client.V1NodeSelector(
                    node_selector_terms=[
                        client.V1NodeSelectorTerm(
                            match_expressions=[
                                client.V1NodeSelectorRequirement(
                                    key="node-role.kubernetes.io/control-plane",
                                    operator="DoesNotExist",
                                )
                            ]
                        )
                    ]
                )
            )
        )
        volumes = [
            client.V1Volume(
                name="tls",
                secret=client.V1SecretVolumeSource(secret_name=self.TLS_SECRET_NAME),
            ),
            client.V1Volume(
                name="app",
                config_map=client.V1ConfigMapVolumeSource(name=self.SCRIPT_CONFIGMAP_NAME),
            ),
        ]
        body = client.V1Deployment(
            metadata=client.V1ObjectMeta(name=name, namespace=self.POLICY_NAMESPACE),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": name}),
                    spec=client.V1PodSpec(
                        containers=[container],
                        affinity=affinity,
                        volumes=volumes,
                    ),
                ),
            ),
        )
        try:
            self.apps_v1.create_namespaced_deployment(namespace=self.POLICY_NAMESPACE, body=body)
            logger.info(f"Created backend deployment '{name}' in '{self.POLICY_NAMESPACE}'.")
        except ApiException as e:
            if e.status == 409:
                existing = self.apps_v1.read_namespaced_deployment(name=name, namespace=self.POLICY_NAMESPACE)
                body.metadata.resource_version = existing.metadata.resource_version
                self.apps_v1.replace_namespaced_deployment(name=name, namespace=self.POLICY_NAMESPACE, body=body)
                logger.info(f"Replaced backend deployment '{name}' in '{self.POLICY_NAMESPACE}'.")
            else:
                raise

    def _ensure_backend_service(self, name: str) -> None:
        """Create the Service that the webhook config points at."""
        body = client.V1Service(
            metadata=client.V1ObjectMeta(name=name, namespace=self.POLICY_NAMESPACE),
            spec=client.V1ServiceSpec(
                selector={"app": name},
                ports=[client.V1ServicePort(port=self.WEBHOOK_BACKEND_PORT, target_port=self.WEBHOOK_BACKEND_PORT)],
            ),
        )
        try:
            self.core_v1.create_namespaced_service(namespace=self.POLICY_NAMESPACE, body=body)
            logger.info(f"Created backend service '{name}' in '{self.POLICY_NAMESPACE}'.")
        except ApiException as e:
            if e.status == 409:
                existing = self.core_v1.read_namespaced_service(name=name, namespace=self.POLICY_NAMESPACE)
                # ClusterIP is immutable once assigned; preserve it on replace.
                body.metadata.resource_version = existing.metadata.resource_version
                body.spec.cluster_ip = existing.spec.cluster_ip
                self.core_v1.replace_namespaced_service(name=name, namespace=self.POLICY_NAMESPACE, body=body)
                logger.info(f"Replaced backend service '{name}' in '{self.POLICY_NAMESPACE}'.")
            else:
                raise

    def _wait_for_deployment_ready(self, name: str, namespace: str, timeout_s: int) -> None:
        """Block until the deployment reports ready_replicas == spec.replicas."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            d = self.apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            desired = d.spec.replicas or 1
            ready = d.status.ready_replicas or 0
            if ready >= desired:
                return
            time.sleep(2)
        raise RuntimeError(f"deployment '{name}' in '{namespace}' did not become Available in {timeout_s}s")

    def _ensure_default_deny_network_policy(self) -> None:
        """Apply (or replace) the default-deny ingress NetworkPolicy in
        the policy namespace. This is what makes the webhook backend
        connections hang to the per-webhook timeout."""
        body = client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name=self.NETWORK_POLICY_NAME, namespace=self.POLICY_NAMESPACE),
            spec=client.V1NetworkPolicySpec(
                pod_selector=client.V1LabelSelector(),
                policy_types=["Ingress"],
            ),
        )
        try:
            self.networking_v1.create_namespaced_network_policy(namespace=self.POLICY_NAMESPACE, body=body)
            logger.info(
                f"Created NetworkPolicy '{self.NETWORK_POLICY_NAME}' (default-deny ingress) "
                f"in '{self.POLICY_NAMESPACE}'."
            )
        except ApiException as e:
            if e.status == 409:
                existing = self.networking_v1.read_namespaced_network_policy(
                    name=self.NETWORK_POLICY_NAME, namespace=self.POLICY_NAMESPACE
                )
                body.metadata.resource_version = existing.metadata.resource_version
                self.networking_v1.replace_namespaced_network_policy(
                    name=self.NETWORK_POLICY_NAME, namespace=self.POLICY_NAMESPACE, body=body
                )
                logger.info(
                    f"Replaced existing NetworkPolicy '{self.NETWORK_POLICY_NAME}' in '{self.POLICY_NAMESPACE}'."
                )
            else:
                raise

    def _ensure_mutating_webhook(self, backend_name: str, ca_bundle_b64: str) -> None:
        """Create the MutatingWebhookConfiguration for one of the backends.
        Each webhook is scoped via ``namespaceSelector`` to only intercept
        the application namespace, so other namespaces are unaffected.

        The configuration name is just ``backend_name`` (no ``sregym-``
        prefix). The benchmark name does not leak into the cluster; an
        agent reading webhook configs sees what looks like a generic
        compliance plane. The cluster-state reconciler in
        ``sregym/service/cluster_state.py`` uses baseline-snapshot
        diffing (not prefix matching) to clean up orphans on teardown,
        so the absence of a prefix does not affect cleanup."""
        webhook_name = f"{backend_name}.compliance.policy"
        config_name = backend_name
        body = client.V1MutatingWebhookConfiguration(
            metadata=client.V1ObjectMeta(name=config_name),
            webhooks=[
                client.V1MutatingWebhook(
                    name=webhook_name,
                    admission_review_versions=["v1"],
                    side_effects="None",
                    failure_policy="Ignore",
                    timeout_seconds=self.WEBHOOK_TIMEOUTS_S[backend_name],
                    rules=[
                        client.V1RuleWithOperations(
                            api_groups=[""],
                            api_versions=["v1"],
                            operations=["CREATE"],
                            resources=["pods"],
                            scope="Namespaced",
                        )
                    ],
                    namespace_selector=client.V1LabelSelector(
                        match_labels={"kubernetes.io/metadata.name": self.namespace},
                    ),
                    client_config=client.AdmissionregistrationV1WebhookClientConfig(
                        service=client.AdmissionregistrationV1ServiceReference(
                            name=backend_name,
                            namespace=self.POLICY_NAMESPACE,
                            port=self.WEBHOOK_BACKEND_PORT,
                            path="/validate",
                        ),
                        ca_bundle=ca_bundle_b64,
                    ),
                ),
            ],
        )
        try:
            self.admissionregistration_v1.create_mutating_webhook_configuration(body=body)
            logger.info(f"Created MutatingWebhookConfiguration '{config_name}'.")
        except ApiException as e:
            if e.status == 409:
                existing = self.admissionregistration_v1.read_mutating_webhook_configuration(name=config_name)
                body.metadata.resource_version = existing.metadata.resource_version
                self.admissionregistration_v1.replace_mutating_webhook_configuration(name=config_name, body=body)
                logger.info(f"Replaced existing MutatingWebhookConfiguration '{config_name}'.")
            else:
                raise

    def _build_decoy_webhook_body(self, spec: dict, ca_bundle_b64: str) -> dict:
        """Build a decoy MutatingWebhookConfiguration body from a
        ``DECOY_WEBHOOKS`` spec.

        Decoys share the same backend Service (one of the real backends),
        CA bundle, failurePolicy, sideEffects, admissionReviewVersions,
        and ``namespaceSelector`` as the real webhooks. They differ only
        in ``metadata.name``, ``webhooks[0].name``, ``rules``, and
        ``objectSelector``. They remain inert because either:

        - their ``rules`` target CRDs/resources that are not installed
          (the cert-manager decoy targets ``cert-manager.io`` CRDs that
          don't exist), or
        - their ``objectSelector`` requires an opt-in label that no
          application pod carries (istio / kyverno / linkerd decoys).

        Pointing all decoys at a real backend Service is intentional;
        an agent comparing webhook configurations cannot distinguish
        real from decoy on the basis of backend or CA bundle alone."""
        decoy_backend = self.WEBHOOK_BACKEND_NAMES[0]
        webhook = {
            "name": spec["webhook_name"],
            "clientConfig": {
                "service": {
                    "name": decoy_backend,
                    "namespace": self.POLICY_NAMESPACE,
                    "path": "/validate",
                    "port": self.WEBHOOK_BACKEND_PORT,
                },
                "caBundle": ca_bundle_b64,
            },
            "rules": spec["rules"],
            "failurePolicy": "Ignore",
            "sideEffects": "None",
            "admissionReviewVersions": ["v1"],
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": self.namespace},
            },
            "timeoutSeconds": self.DECOY_WEBHOOK_TIMEOUT_SECONDS,
        }
        if spec.get("object_selector") is not None:
            webhook["objectSelector"] = spec["object_selector"]
        return {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": spec["name"]},
            "webhooks": [webhook],
        }

    def _install_decoy_webhooks(self, ca_bundle_b64: str) -> None:
        """Install all ``DECOY_WEBHOOKS``. Idempotent."""
        logger.info(f"Installing {len(self.DECOY_WEBHOOKS)} decoy MutatingWebhookConfigurations.")
        for spec in self.DECOY_WEBHOOKS:
            body = self._build_decoy_webhook_body(spec, ca_bundle_b64)
            try:
                self.admissionregistration_v1.create_mutating_webhook_configuration(body=body)
                logger.info(f"Created decoy MutatingWebhookConfiguration '{spec['name']}'.")
            except ApiException as e:
                if e.status == 409:
                    existing = self.admissionregistration_v1.read_mutating_webhook_configuration(name=spec["name"])
                    body["metadata"]["resourceVersion"] = existing.metadata.resource_version
                    self.admissionregistration_v1.replace_mutating_webhook_configuration(name=spec["name"], body=body)
                    logger.info(f"Replaced existing decoy MutatingWebhookConfiguration '{spec['name']}'.")
                else:
                    raise

    def _remove_decoy_webhooks(self) -> None:
        """Delete all ``DECOY_WEBHOOKS``. 404s are tolerated (already gone)."""
        logger.info("Removing decoy MutatingWebhookConfigurations.")
        for spec in self.DECOY_WEBHOOKS:
            try:
                self.admissionregistration_v1.delete_mutating_webhook_configuration(name=spec["name"])
                logger.info(f"Deleted decoy MutatingWebhookConfiguration '{spec['name']}'.")
            except ApiException as e:
                if e.status == 404:
                    logger.info(f"Decoy MutatingWebhookConfiguration '{spec['name']}' already absent.")
                else:
                    raise

    def _delete_target_pod(self) -> None:
        """Delete the running pod of the target deployment so the
        ReplicaSet's recreate attempt hits the broken admission path."""
        pods = self.core_v1.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=f"io.kompose.service={self.TARGET_DEPLOYMENT}",
        )
        if not pods.items:
            logger.warning(
                f"No pods found for deployment '{self.TARGET_DEPLOYMENT}' in "
                f"'{self.namespace}'. Skipping the deletion trigger; the symptom "
                "will appear once any other event prompts a new pod creation."
            )
            return
        for pod in pods.items:
            try:
                self.core_v1.delete_namespaced_pod(
                    name=pod.metadata.name,
                    namespace=self.namespace,
                    grace_period_seconds=0,
                )
                logger.info(
                    f"Deleted pod '{pod.metadata.name}' from '{self.TARGET_DEPLOYMENT}' "
                    f"in '{self.namespace}' to trigger admission failure on recreation."
                )
            except ApiException as e:
                if e.status != 404:
                    raise

    def _wait_for_symptom(self) -> None:
        """Poll until the target deployment reports
        ``ready_replicas < spec.replicas`` (or until the timeout).
        This confirms the ReplicaSet's attempt to recreate the pod was
        blocked by admission, which is what makes the fault
        user-observable."""
        deadline = time.monotonic() + self.SYMPTOM_TIMEOUT_S
        while time.monotonic() < deadline:
            d = self.apps_v1.read_namespaced_deployment(name=self.TARGET_DEPLOYMENT, namespace=self.namespace)
            desired = d.spec.replicas or 1
            ready = d.status.ready_replicas or 0
            if ready < desired:
                return
            time.sleep(self.SYMPTOM_POLL_INTERVAL_S)
        raise RuntimeError(
            f"Symptom did not manifest within {self.SYMPTOM_TIMEOUT_S}s: "
            f"'{self.TARGET_DEPLOYMENT}' still shows full ready_replicas. The "
            "ReplicaSet may have recreated the pod successfully, indicating the "
            "fault is not triggering admission failure as expected."
        )

    def _wait_for_recovery(self) -> None:
        """Poll until the target deployment converges back to fully ready
        after the NetworkPolicy is opened."""
        deadline = time.monotonic() + self.RECOVERY_TIMEOUT_S
        while time.monotonic() < deadline:
            d = self.apps_v1.read_namespaced_deployment(name=self.TARGET_DEPLOYMENT, namespace=self.namespace)
            desired = d.spec.replicas or 1
            ready = d.status.ready_replicas or 0
            if ready >= desired:
                return
            time.sleep(self.RECOVERY_POLL_INTERVAL_S)
        raise RuntimeError(
            f"Recovery did not converge within {self.RECOVERY_TIMEOUT_S}s: "
            f"'{self.TARGET_DEPLOYMENT}' still under-replicated. The NetworkPolicy "
            "was deleted, but admission may still be failing for another reason."
        )
