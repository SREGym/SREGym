"""Problem: a MutatingWebhookConfiguration silently rewrites pod memory
requests and limits, causing OOMKill on every new pod in the social-network
namespace.

The target Deployment (``nginx-thrift``) is first patched with explicit,
legitimate-looking memory resources (``requests: 128Mi``, ``limits: 256Mi``)
so the spec carries real values — an agent inspecting the Deployment cannot
rule the webhook in by spotting an empty ``resources`` block. The webhook
backend is reachable and TLS is valid, so admission succeeds. At pod CREATE
time the webhook overwrites both ``requests.memory`` and ``limits.memory``
to ``16Mi`` (keeping ``requests <= limits`` so the apiserver accepts the
mutated pod), and the container is OOMKilled on startup. The only diagnostic
clue is the gap between the Deployment spec (128Mi / 256Mi) and the running
pod resources (16Mi / 16Mi).

Several inert decoy MutatingWebhookConfigurations (cert-manager, istio,
kyverno, linkerd) are installed alongside the real one so the cluster's
admission stack looks like a realistic production setup; the agent has to
read each webhook's rules and clientConfig to identify the active one.

Valid mitigations include deleting the active MutatingWebhookConfiguration
(``pod-policy.platform.k8s.io``) followed by a rolling restart to replace
the OOMKilled pods.
"""

import base64
import json
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.deployment_readiness import DeploymentReadinessOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class MutatingWebhookResourceLimits(Problem):
    """Pre-patch nginx-thrift with legitimate-looking memory resources, then
    deploy a mutating admission webhook that silently rewrites both
    ``requests.memory`` and ``limits.memory`` of every new pod to
    ``INJECTED_MEMORY``."""

    WEBHOOK_NAME = "pod-policy.platform.k8s.io"
    BACKEND_SVC_NAME = "platform-policy-controller"
    BACKEND_SVC_NAMESPACE = "platform-ops"
    BACKEND_DEPLOYMENT_NAME = "platform-policy-controller"
    # Spec values applied to the target Deployment before the webhook is armed,
    # so the agent sees plausible production-grade resource values when they
    # inspect the Deployment.
    SPEC_MEMORY_REQUEST = "128Mi"
    SPEC_MEMORY_LIMIT = "256Mi"
    # Value the webhook stamps into both requests.memory and limits.memory at
    # CREATE time. Equal request and limit keeps the mutated pod admissible
    # (requests <= limits) while still triggering OOMKill on real workloads.
    INJECTED_MEMORY = "16Mi"

    # All decoy webhooks share this namespaceSelector. No namespace in the
    # cluster carries the label, so the decoys are never consulted regardless
    # of their rules. failurePolicy=Ignore is a second layer of safety in case
    # an unrelated controller ever happens to apply the label.
    DECOY_NAMESPACE_SELECTOR = {"matchLabels": {"sregym.io/decoy-target": "true"}}

    # Inert decoy MutatingWebhookConfigurations installed alongside the real
    # fault so an agent surveying the cluster sees a realistic policy/mesh
    # stack rather than a single suspicious webhook. Names, webhook FQDNs,
    # and rules mirror the canonical configurations shipped by each project
    # so the real fault hides among them.
    DECOY_WEBHOOKS = (
        {
            "name": "cert-manager-webhook",
            "webhook_name": "webhook.cert-manager.io",
            "service_name": "cert-manager-webhook",
            "service_namespace": "cert-manager",
            "rules": [{
                "apiGroups": ["cert-manager.io"],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["certificates", "issuers"],
                "scope": "*",
            }],
        },
        {
            "name": "istio-sidecar-injector",
            "webhook_name": "rev.namespace.sidecar-injector.istio.io",
            "service_name": "istiod",
            "service_namespace": "istio-system",
            "rules": [{
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE"],
                "resources": ["pods"],
                "scope": "Namespaced",
            }],
        },
        {
            "name": "kyverno-resource-mutating-webhook-cfg",
            "webhook_name": "mutate.kyverno.svc",
            "service_name": "kyverno-svc",
            "service_namespace": "kyverno",
            "rules": [{
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE", "UPDATE"],
                "resources": ["pods"],
                "scope": "Namespaced",
            }],
        },
        {
            "name": "linkerd-proxy-injector-webhook-config",
            "webhook_name": "linkerd-proxy-injector.linkerd.io",
            "service_name": "linkerd-proxy-injector",
            "service_namespace": "linkerd",
            "rules": [{
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["CREATE"],
                "resources": ["pods"],
                "scope": "Namespaced",
            }],
        },
    )

    def __init__(self):
        self.faulty_service = "nginx-thrift"
        self.app = SocialNetwork()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.namespace = self.app.namespace
        self.kubectl = KubeCtl()
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()
        self.ca_bundle = None

        self.root_cause = self.build_structured_root_cause(
            component=f"MutatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            namespace=self.namespace,
            description=(
                f"The fault is the cluster-scoped MutatingWebhookConfiguration named `{self.WEBHOOK_NAME}`. "
                f"It intercepts all pod CREATE operations in the `{self.namespace}` namespace and injects "
                f"a JSON patch that overwrites the first container's `resources.requests.memory` and "
                f"`resources.limits.memory` to `{self.INJECTED_MEMORY}`. The webhook backend server that "
                "executes the patch is not the fault — it is functioning exactly as configured. The fault "
                "is the webhook configuration itself being present. The Deployment spec reports legitimate "
                f"memory values (`requests: {self.SPEC_MEMORY_REQUEST}`, `limits: {self.SPEC_MEMORY_LIMIT}`); "
                f"the actual running pods have `requests: {self.INJECTED_MEMORY}` / "
                f"`limits: {self.INJECTED_MEMORY}` and are immediately OOMKilled on startup. The discrepancy "
                "between the Deployment spec and the running pod resources is the diagnostic signal."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = DeploymentReadinessOracle(problem=self)

    def _run(self, args, **kwargs):
        return subprocess.run(args, check=True, text=True, **kwargs)

    def _generate_tls_material(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            ca_key = d / "ca.key"
            ca_crt = d / "ca.crt"
            server_key = d / "server.key"
            server_csr = d / "server.csr"
            server_crt = d / "server.crt"
            ext = d / "server.ext"

            self._run(["openssl", "genrsa", "-out", str(ca_key), "2048"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._run([
                "openssl", "req", "-x509", "-new", "-nodes",
                "-key", str(ca_key), "-sha256", "-days", "365",
                "-subj", "/CN=platform-policy-ca",
                "-out", str(ca_crt),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            self._run(["openssl", "genrsa", "-out", str(server_key), "2048"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._run([
                "openssl", "req", "-new",
                "-key", str(server_key),
                "-subj", f"/CN={self.BACKEND_SVC_NAME}.{self.BACKEND_SVC_NAMESPACE}.svc",
                "-out", str(server_csr),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            ext.write_text(
                f"subjectAltName=DNS:{self.BACKEND_SVC_NAME}.{self.BACKEND_SVC_NAMESPACE}.svc,"
                f"DNS:{self.BACKEND_SVC_NAME}.{self.BACKEND_SVC_NAMESPACE}.svc.cluster.local\n"
            )

            self._run([
                "openssl", "x509", "-req",
                "-in", str(server_csr),
                "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                "-out", str(server_crt), "-days", "365", "-sha256",
                "-extfile", str(ext),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            return {
                "tls_crt_b64": base64.b64encode(server_crt.read_bytes()).decode(),
                "tls_key_b64": base64.b64encode(server_key.read_bytes()).decode(),
                "ca_bundle": base64.b64encode(ca_crt.read_bytes()).decode(),
            }

    def _ensure_webhook_backend(self):
        print("[Backend] Deploying admission webhook backend")
        material = self._generate_tls_material()
        self.ca_bundle = material["ca_bundle"]

        server_code = r'''
import base64
import json
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

MEMORY = "__MEMORY_VALUE__"

def build_patch(resources):
    """Emit JSON Patch ops that stamp MEMORY into both
    resources.requests.memory and resources.limits.memory on container[0].
    RFC 6902 "add" semantics: replaces the value if the target already exists,
    so this works whether the field is absent or already populated."""
    ops = []
    if resources is None:
        ops.append({
            "op": "add",
            "path": "/spec/containers/0/resources",
            "value": {"requests": {"memory": MEMORY}, "limits": {"memory": MEMORY}},
        })
        return ops
    if resources.get("requests") is None:
        ops.append({"op": "add", "path": "/spec/containers/0/resources/requests",
                    "value": {"memory": MEMORY}})
    else:
        ops.append({"op": "add", "path": "/spec/containers/0/resources/requests/memory",
                    "value": MEMORY})
    if resources.get("limits") is None:
        ops.append({"op": "add", "path": "/spec/containers/0/resources/limits",
                    "value": {"memory": MEMORY}})
    else:
        ops.append({"op": "add", "path": "/spec/containers/0/resources/limits/memory",
                    "value": MEMORY})
    return ops

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0") or 0)
        raw = self.rfile.read(length)
        uid = ""
        patch = []
        try:
            body = json.loads(raw)
            uid = body.get("request", {}).get("uid", "")
            containers = body.get("request", {}).get("object", {}).get("spec", {}).get("containers", [])
            if containers:
                patch = build_patch(containers[0].get("resources"))
        except Exception:
            pass

        resp = {"uid": uid, "allowed": True}
        if patch:
            resp["patchType"] = "JSONPatch"
            resp["patch"] = base64.b64encode(json.dumps(patch).encode()).decode()
        response = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": resp,
        }
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return

httpd = HTTPServer(("0.0.0.0", 8443), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/certs/tls.crt", "/certs/tls.key")
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
httpd.serve_forever()
'''
        server_code = server_code.replace("__MEMORY_VALUE__", self.INJECTED_MEMORY)

        manifest = f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {self.BACKEND_SVC_NAMESPACE}
---
apiVersion: v1
kind: Secret
metadata:
  name: {self.BACKEND_SVC_NAME}-tls
  namespace: {self.BACKEND_SVC_NAMESPACE}
type: kubernetes.io/tls
data:
  tls.crt: {material["tls_crt_b64"]}
  tls.key: {material["tls_key_b64"]}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {self.BACKEND_SVC_NAME}-server
  namespace: {self.BACKEND_SVC_NAMESPACE}
data:
  server.py: |
{textwrap.indent(server_code.strip(), "    ")}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {self.BACKEND_DEPLOYMENT_NAME}
  namespace: {self.BACKEND_SVC_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {self.BACKEND_DEPLOYMENT_NAME}
  template:
    metadata:
      labels:
        app: {self.BACKEND_DEPLOYMENT_NAME}
    spec:
      containers:
      - name: webhook
        image: python:3.12-alpine
        imagePullPolicy: IfNotPresent
        command: ["python", "/app/server.py"]
        ports:
        - containerPort: 8443
        volumeMounts:
        - name: server-code
          mountPath: /app
        - name: tls
          mountPath: /certs
          readOnly: true
        resources:
          requests:
            cpu: 10m
            memory: 32Mi
          limits:
            cpu: 200m
            memory: 128Mi
      volumes:
      - name: server-code
        configMap:
          name: {self.BACKEND_SVC_NAME}-server
      - name: tls
        secret:
          secretName: {self.BACKEND_SVC_NAME}-tls
---
apiVersion: v1
kind: Service
metadata:
  name: {self.BACKEND_SVC_NAME}
  namespace: {self.BACKEND_SVC_NAMESPACE}
spec:
  selector:
    app: {self.BACKEND_DEPLOYMENT_NAME}
  ports:
  - name: https
    port: 443
    targetPort: 8443
"""

        self._run(["kubectl", "apply", "-f", "-"], input=manifest)
        self._run([
            "kubectl", "-n", self.BACKEND_SVC_NAMESPACE,
            "rollout", "status", f"deployment/{self.BACKEND_DEPLOYMENT_NAME}",
            "--timeout=180s",
        ])

    def _build_webhook_body(self) -> dict:
        if not self.ca_bundle:
            raise RuntimeError("ca_bundle not initialized; call _ensure_webhook_backend first")

        return {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": self.WEBHOOK_NAME},
            "webhooks": [
                {
                    "name": self.WEBHOOK_NAME,
                    "clientConfig": {
                        "service": {
                            "name": self.BACKEND_SVC_NAME,
                            "namespace": self.BACKEND_SVC_NAMESPACE,
                            "path": "/mutate",
                            "port": 443,
                        },
                        "caBundle": self.ca_bundle,
                    },
                    "rules": [
                        {
                            "apiGroups": [""],
                            "apiVersions": ["v1"],
                            "operations": ["CREATE"],
                            "resources": ["pods"],
                            "scope": "Namespaced",
                        }
                    ],
                    "failurePolicy": "Fail",
                    "sideEffects": "None",
                    "admissionReviewVersions": ["v1"],
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": self.namespace},
                    },
                    "timeoutSeconds": 5,
                }
            ],
        }

    def _build_decoy_webhook_body(self, spec: dict) -> dict:
        """Build a decoy MutatingWebhookConfiguration body from a DECOY_WEBHOOKS spec.

        Decoys reuse the real backend's CA bundle (saves a second OpenSSL run
        and makes them visually indistinguishable from the real one to an
        agent eyeballing caBundle values). They point at services in
        namespaces that don't exist; the apiserver never resolves them
        because DECOY_NAMESPACE_SELECTOR matches no namespace, and
        failurePolicy=Ignore guarantees no impact even if it did.
        """
        if not self.ca_bundle:
            raise RuntimeError("ca_bundle not initialized; call _ensure_webhook_backend first")

        return {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": spec["name"]},
            "webhooks": [
                {
                    "name": spec["webhook_name"],
                    "clientConfig": {
                        "service": {
                            "name": spec["service_name"],
                            "namespace": spec["service_namespace"],
                            "path": "/mutate",
                            "port": 443,
                        },
                        "caBundle": self.ca_bundle,
                    },
                    "rules": spec["rules"],
                    "failurePolicy": "Ignore",
                    "sideEffects": "None",
                    "admissionReviewVersions": ["v1"],
                    "namespaceSelector": self.DECOY_NAMESPACE_SELECTOR,
                    "timeoutSeconds": 1,
                }
            ],
        }

    def _install_decoy_webhooks(self):
        """Install all DECOY_WEBHOOKS. Idempotent: existing decoys are replaced
        in-place so re-running inject_fault never errors on the second pass."""
        print(f"[Decoys] Installing {len(self.DECOY_WEBHOOKS)} decoy MutatingWebhookConfigurations")
        for spec in self.DECOY_WEBHOOKS:
            body = self._build_decoy_webhook_body(spec)
            try:
                self.admission_api.create_mutating_webhook_configuration(body=body)
                print(f"  Created decoy: {spec['name']}")
            except ApiException as e:
                if e.status == 409:
                    existing = self.admission_api.read_mutating_webhook_configuration(name=spec["name"])
                    body["metadata"]["resourceVersion"] = existing.metadata.resource_version
                    self.admission_api.replace_mutating_webhook_configuration(name=spec["name"], body=body)
                    print(f"  Replaced decoy: {spec['name']}")
                else:
                    raise

    def _remove_decoy_webhooks(self):
        """Delete all DECOY_WEBHOOKS. 404s are tolerated (already gone)."""
        print("[Decoys] Removing decoy MutatingWebhookConfigurations")
        for spec in self.DECOY_WEBHOOKS:
            try:
                self.admission_api.delete_mutating_webhook_configuration(name=spec["name"])
                print(f"  Deleted decoy: {spec['name']}")
            except ApiException as e:
                if e.status == 404:
                    print(f"  Decoy {spec['name']} already absent")
                else:
                    raise

    def _patch_target_deployment_resources(self):
        """Patch the target Deployment's first container with explicit memory
        requests and limits so its spec carries plausible production values.

        The container is identified by name (looked up from the live
        Deployment) so the strategic-merge patch merges into the existing
        container rather than appending a new one. This runs before the
        webhook is armed so the rolling restart caused by the patch completes
        with healthy pods; the webhook only ever sees the *next* CREATE.
        """
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        if deployment is None:
            raise RuntimeError(
                f"Deployment '{self.faulty_service}' not found in namespace '{self.namespace}'"
            )
        containers = deployment.spec.template.spec.containers
        if not containers:
            raise RuntimeError(
                f"Deployment '{self.faulty_service}' has no containers"
            )
        container_name = containers[0].name

        print(
            f"[Spec Pre-patch] Setting {self.faulty_service} container "
            f"'{container_name}' memory requests={self.SPEC_MEMORY_REQUEST}, "
            f"limits={self.SPEC_MEMORY_LIMIT}"
        )
        patch_body = json.dumps({
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container_name,
                                "resources": {
                                    "requests": {"memory": self.SPEC_MEMORY_REQUEST},
                                    "limits": {"memory": self.SPEC_MEMORY_LIMIT},
                                },
                            }
                        ]
                    }
                }
            }
        })
        self._run([
            "kubectl", "-n", self.namespace,
            "patch", "deployment", self.faulty_service,
            "--type=strategic", "-p", patch_body,
        ])
        self._run([
            "kubectl", "-n", self.namespace,
            "rollout", "status", f"deployment/{self.faulty_service}",
            "--timeout=180s",
        ])

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        # Pre-patch the target Deployment so its spec carries explicit, realistic
        # memory resources before the webhook is armed. Without this, an agent
        # can rule the webhook in by spotting an empty `resources` block on the
        # spec vs. a populated one on the running pod. With the spec carrying
        # 128Mi/256Mi, only the *value* gap (spec 128/256 vs. pod 16/16) leaks
        # the mutation.
        self._patch_target_deployment_resources()

        self._ensure_webhook_backend()

        # Install decoys before the real webhook so the cluster's admission
        # surface looks like a real policy/mesh stack the moment the fault is
        # armed. Decoys are inert (namespaceSelector never matches,
        # failurePolicy=Ignore), so install order has no functional effect.
        self._install_decoy_webhooks()

        webhook = self._build_webhook_body()
        try:
            self.admission_api.create_mutating_webhook_configuration(body=webhook)
            print(f"Created MutatingWebhookConfiguration: {self.WEBHOOK_NAME}")
        except ApiException as e:
            if e.status == 409:
                print(f"MutatingWebhookConfiguration {self.WEBHOOK_NAME} exists; replacing")
                existing = self.admission_api.read_mutating_webhook_configuration(name=self.WEBHOOK_NAME)
                webhook["metadata"]["resourceVersion"] = existing.metadata.resource_version
                self.admission_api.replace_mutating_webhook_configuration(name=self.WEBHOOK_NAME, body=webhook)
            else:
                raise

        time.sleep(8)

        pods = self.core_api.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=f"service={self.faulty_service}",
        )
        if not pods.items:
            raise RuntimeError(f"No pods found for service '{self.faulty_service}' in namespace '{self.namespace}'")

        target = pods.items[0].metadata.name
        self.core_api.delete_namespaced_pod(
            name=target,
            namespace=self.namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0),
        )
        print(
            f"Deleted pod {target}; replacement pod will be mutated to "
            f"requests={self.INJECTED_MEMORY}, limits={self.INJECTED_MEMORY}"
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        try:
            self.admission_api.delete_mutating_webhook_configuration(name=self.WEBHOOK_NAME)
            print(f"Deleted MutatingWebhookConfiguration: {self.WEBHOOK_NAME}")
        except ApiException as e:
            if e.status == 404:
                print(f"MutatingWebhookConfiguration {self.WEBHOOK_NAME} already absent")
            else:
                raise

        self._remove_decoy_webhooks()

        subprocess.run(
            ["kubectl", "delete", "namespace", self.BACKEND_SVC_NAMESPACE, "--ignore-not-found"],
            check=False,
            text=True,
        )

        self._run([
            "kubectl", "rollout", "restart",
            f"deployment/{self.faulty_service}", "-n", self.namespace,
        ])
        self._run([
            "kubectl", "rollout", "status",
            f"deployment/{self.faulty_service}", "-n", self.namespace,
            "--timeout=120s",
        ])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
