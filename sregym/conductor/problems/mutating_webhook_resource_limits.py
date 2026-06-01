"""Problem: a MutatingWebhookConfiguration silently overwrites pod memory limits,
causing OOMKill on every new pod in the social-network namespace.

The webhook backend is reachable and TLS is valid, so admission succeeds — but
every pod is mutated at creation time. The Deployment spec looks healthy. The
actual running pods have an injected memory limit and crash immediately. The gap
between spec and runtime is the diagnostic clue.

Valid mitigations include deleting the MutatingWebhookConfiguration or patching
it to change failurePolicy to Ignore, followed by a rolling restart to replace
the OOMKilled pods.
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
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class MutatingWebhookResourceLimits(Problem):
    """Deploy a mutating admission webhook that silently caps pod memory to INJECTED_MEMORY_LIMIT."""

    WEBHOOK_NAME = "pod-policy.platform.k8s.io"
    BACKEND_SVC_NAME = "platform-policy-controller"
    BACKEND_SVC_NAMESPACE = "platform-ops"
    BACKEND_DEPLOYMENT_NAME = "platform-policy-controller"
    INJECTED_MEMORY_LIMIT = "16Mi"

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
                f"a JSON patch that overwrites the first container's memory limit to `{self.INJECTED_MEMORY_LIMIT}`. "
                "The webhook backend server that executes the patch is not the fault — it is functioning "
                "exactly as configured. The fault is the webhook configuration itself being present. "
                "The Deployment spec is untouched and reports correct resource limits; "
                f"the actual running pods have `{self.INJECTED_MEMORY_LIMIT}` and are immediately OOMKilled on startup."
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
                resources = containers[0].get("resources")
                if resources is None:
                    patch = [{"op": "add", "path": "/spec/containers/0/resources",
                              "value": {"limits": {"memory": "__MEMORY_LIMIT__"}}}]
                elif resources.get("limits") is None:
                    patch = [{"op": "add", "path": "/spec/containers/0/resources/limits",
                              "value": {"memory": "__MEMORY_LIMIT__"}}]
                else:
                    patch = [{"op": "add", "path": "/spec/containers/0/resources/limits/memory",
                              "value": "__MEMORY_LIMIT__"}]
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
        server_code = server_code.replace("__MEMORY_LIMIT__", self.INJECTED_MEMORY_LIMIT)

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

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        self._ensure_webhook_backend()

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
        print(f"Deleted pod {target}; replacement pod will be mutated to {self.INJECTED_MEMORY_LIMIT} memory limit")
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
