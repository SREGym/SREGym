"""Problem: admission webhook TLS trust mismatch blocks pod admission in an app namespace.

This models a production-style Kubernetes failure during webhook certificate
rotation: a ValidatingWebhookConfiguration with ``failurePolicy: Fail`` points
at a reachable HTTPS webhook backend, but its ``caBundle`` is stale/wrong.
The kube-apiserver therefore cannot verify the webhook server's TLS certificate
and rejects pod CREATE admission requests.

In SREGym we scope the failure to a single application namespace via
``namespaceSelector`` and then delete one pod of a single-replica deployment
(e.g. ``recommendation`` in hotel-reservation). The ReplicaSet's recreate
attempt hits the webhook and is rejected with a TLS/x509 admission error, so the
deployment stays under-replicated even though its spec, image, service, and
resources are healthy.

Valid mitigations include deleting the webhook config, changing
``failurePolicy`` to ``Ignore``, or repairing the webhook TLS trust chain by
restoring a valid ``caBundle``.
"""

import base64
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
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class AdmissionWebhookTLSMismatch(Problem):
    """Inject a reachable admission webhook backend but configure the
    ValidatingWebhookConfiguration with a stale/wrong caBundle."""

    APPS = {
        "hotel_reservation": HotelReservation,
        "social_network": SocialNetwork,
        "astronomy_shop": AstronomyShop,
    }

    WEBHOOK_NAME = "pod-policy.validation.k8s.io"
    BACKEND_SVC_NAME = "pod-policy-webhook"
    BACKEND_SVC_NAMESPACE = "policy-system"
    BACKEND_DEPLOYMENT_NAME = "pod-policy-webhook"

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "recommendation"):
        if app_name not in self.APPS:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.app_name = app_name
        self.faulty_service = faulty_service
        app = self.APPS[app_name]()
        super().__init__(app=app)

        self.kubectl = KubeCtl()
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()
        self.wrong_ca_bundle = None

        self.root_cause = self.build_structured_root_cause(
            component=f"ValidatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            namespace=self.namespace,
            description=(
                f"A cluster-scoped ValidatingWebhookConfiguration named `{self.WEBHOOK_NAME}` has been installed "
                f"with `failurePolicy: Fail` and a `namespaceSelector` scoped to the `{self.namespace}` namespace. "
                f"The webhook intercepts pod CREATE operations and points to the reachable HTTPS service "
                f"`{self.BACKEND_SVC_NAMESPACE}/{self.BACKEND_SVC_NAME}`, but the webhook configuration contains "
                "a stale/wrong `caBundle`. As a result, the kube-apiserver cannot verify the webhook server's TLS "
                "certificate and rejects pod creation with a `failed calling webhook` / `x509` certificate error. "
                f"The ReplicaSet controlling the `{self.faulty_service}` deployment cannot recreate pods after "
                "they are deleted, leaving the deployment under-replicated. The deployment itself is healthy; it is "
                "an innocent victim of a cluster-scoped admission TLS trust dependency. The mitigation is to remove "
                "the broken ValidatingWebhookConfiguration, change `failurePolicy` to `Ignore`, or repair the "
                "webhook TLS trust chain by restoring the correct `caBundle`."
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
            wrong_ca_key = d / "wrong-ca.key"
            wrong_ca_crt = d / "wrong-ca.crt"
            server_key = d / "server.key"
            server_csr = d / "server.csr"
            server_crt = d / "server.crt"
            ext = d / "server.ext"

            self._run(["openssl", "genrsa", "-out", str(ca_key), "2048"], stdout=subprocess.DEVNULL)
            self._run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-new",
                    "-nodes",
                    "-key",
                    str(ca_key),
                    "-sha256",
                    "-days",
                    "365",
                    "-subj",
                    "/CN=platform-services-ca",
                    "-out",
                    str(ca_crt),
                ],
                stdout=subprocess.DEVNULL,
            )

            self._run(["openssl", "genrsa", "-out", str(wrong_ca_key), "2048"], stdout=subprocess.DEVNULL)
            self._run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-new",
                    "-nodes",
                    "-key",
                    str(wrong_ca_key),
                    "-sha256",
                    "-days",
                    "365",
                    "-subj",
                    "/CN=cluster-policy-ca",
                    "-out",
                    str(wrong_ca_crt),
                ],
                stdout=subprocess.DEVNULL,
            )

            self._run(["openssl", "genrsa", "-out", str(server_key), "2048"], stdout=subprocess.DEVNULL)
            self._run(
                [
                    "openssl",
                    "req",
                    "-new",
                    "-key",
                    str(server_key),
                    "-subj",
                    f"/CN={self.BACKEND_SVC_NAME}.{self.BACKEND_SVC_NAMESPACE}.svc",
                    "-out",
                    str(server_csr),
                ],
                stdout=subprocess.DEVNULL,
            )

            ext.write_text(
                f"subjectAltName=DNS:{self.BACKEND_SVC_NAME}.{self.BACKEND_SVC_NAMESPACE}.svc,"
                f"DNS:{self.BACKEND_SVC_NAME}.{self.BACKEND_SVC_NAMESPACE}.svc.cluster.local\n"
            )

            self._run(
                [
                    "openssl",
                    "x509",
                    "-req",
                    "-in",
                    str(server_csr),
                    "-CA",
                    str(ca_crt),
                    "-CAkey",
                    str(ca_key),
                    "-CAcreateserial",
                    "-out",
                    str(server_crt),
                    "-days",
                    "365",
                    "-sha256",
                    "-extfile",
                    str(ext),
                ],
                stdout=subprocess.DEVNULL,
            )

            return {
                "tls_crt_b64": base64.b64encode(server_crt.read_bytes()).decode(),
                "tls_key_b64": base64.b64encode(server_key.read_bytes()).decode(),
                "wrong_ca_bundle": base64.b64encode(wrong_ca_crt.read_bytes()).decode(),
            }

    def _ensure_webhook_backend(self):
        print("[Backend] Creating reachable HTTPS webhook backend with intentionally mismatched CA trust")
        material = self._generate_tls_material()
        self.wrong_ca_bundle = material["wrong_ca_bundle"]

        server_code = r"""
import json
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0") or 0)
        raw = self.rfile.read(length)
        uid = ""
        try:
            uid = json.loads(raw).get("request", {}).get("uid", "")
        except Exception:
            pass

        response = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {"uid": uid, "allowed": True},
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
"""

        manifest = f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {self.BACKEND_SVC_NAMESPACE}
---
apiVersion: v1
kind: Secret
metadata:
  name: pod-policy-webhook-tls
  namespace: {self.BACKEND_SVC_NAMESPACE}
type: kubernetes.io/tls
data:
  tls.crt: {material["tls_crt_b64"]}
  tls.key: {material["tls_key_b64"]}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: pod-policy-webhook-server
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
          name: pod-policy-webhook-server
      - name: tls
        secret:
          secretName: pod-policy-webhook-tls
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
        self._run(
            [
                "kubectl",
                "-n",
                self.BACKEND_SVC_NAMESPACE,
                "rollout",
                "status",
                f"deployment/{self.BACKEND_DEPLOYMENT_NAME}",
                "--timeout=180s",
            ]
        )

    def _build_webhook_body(self) -> dict:
        if not self.wrong_ca_bundle:
            raise RuntimeError("wrong_ca_bundle is not initialized; call _ensure_webhook_backend first")

        return {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingWebhookConfiguration",
            "metadata": {"name": self.WEBHOOK_NAME},
            "webhooks": [
                {
                    "name": self.WEBHOOK_NAME,
                    "clientConfig": {
                        "service": {
                            "name": self.BACKEND_SVC_NAME,
                            "namespace": self.BACKEND_SVC_NAMESPACE,
                            "path": "/validate",
                            "port": 443,
                        },
                        "caBundle": self.wrong_ca_bundle,
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

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        self._ensure_webhook_backend()

        webhook = self._build_webhook_body()
        try:
            self.admission_api.create_validating_webhook_configuration(body=webhook)
            print(f"Created ValidatingWebhookConfiguration: {self.WEBHOOK_NAME}")
        except ApiException as e:
            if e.status == 409:
                print(f"ValidatingWebhookConfiguration {self.WEBHOOK_NAME} exists; replacing")
                existing = self.admission_api.read_validating_webhook_configuration(name=self.WEBHOOK_NAME)
                webhook["metadata"]["resourceVersion"] = existing.metadata.resource_version
                self.admission_api.replace_validating_webhook_configuration(name=self.WEBHOOK_NAME, body=webhook)
            else:
                raise

        time.sleep(2)

        pods = self.core_api.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=f"io.kompose.service={self.faulty_service}",
        )
        if not pods.items:
            raise RuntimeError(f"No pods found for service '{self.faulty_service}' in namespace '{self.namespace}'")

        target = pods.items[0].metadata.name
        self.core_api.delete_namespaced_pod(
            name=target,
            namespace=self.namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0),
        )
        print(f"Deleted pod {target}; ReplicaSet recreate will be blocked by webhook TLS trust failure")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        try:
            self.admission_api.delete_validating_webhook_configuration(name=self.WEBHOOK_NAME)
            print(f"Deleted ValidatingWebhookConfiguration: {self.WEBHOOK_NAME}")
        except ApiException as e:
            if e.status == 404:
                print(f"ValidatingWebhookConfiguration {self.WEBHOOK_NAME} already absent")
            else:
                raise

        subprocess.run(
            ["kubectl", "delete", "namespace", self.BACKEND_SVC_NAMESPACE, "--ignore-not-found"],
            check=False,
            text=True,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
