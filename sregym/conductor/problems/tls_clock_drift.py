"""Portable TLS validation-clock drift, without changing a node or host clock.

OpenSSL's verification time models the clock used by an outbound TLS client.
The dependency uses a real TLS handshake; failure makes the frontend Unready.
This is an explicitly versioned replacement for the native clock task on Kind.
"""

import base64
import subprocess
import tempfile
import time
from pathlib import Path

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.tls_clock_drift_mitigation import TLSClockDriftMitigationOracle
from sregym.conductor.problems.node_clock_drift import NodeClockDriftHotelReservation
from sregym.service.runtime_images import TLS_CLIENT_IMAGE
from sregym.utils.decorators import mark_fault_injected

TLS_HOSTNAME = "hotel-reservation.local"
TLS_SERVICE = "frontend-tls"
TLS_SECRET = "hotel-frontend-tls"
TLS_CA = "hotel-frontend-ca"
CLOCK_CONFIG = "frontend-client-settings"
CLOCK_KEY = "clock-offset-seconds"
TLS_CONTAINER = "tls-health-check"

# This is the actual client operation, shared by its readiness check and fresh
# verification pods. Neither success markers nor a log string establish health.
TLS_VERIFY_COMMAND = (
    "offset=$(cat /etc/client-clock/clock-offset-seconds) || exit 1; "
    'printf "%s\\n" "$offset" | grep -Eq "^[+-]?[0-9]{1,10}$" || exit 1; '
    'epoch=$(expr "$(date +%s)" + "$offset") || exit 1; '
    'printf "GET / HTTP/1.0\\r\\n\\r\\n" | '
    f"timeout 8 openssl s_client -connect {TLS_SERVICE}:9443 "
    f"-servername {TLS_HOSTNAME} -verify_hostname {TLS_HOSTNAME} "
    '-verify_return_error -CAfile /etc/tls-ca/ca.crt -attime "$epoch" -quiet'
)


def tls_client_container() -> dict:
    return {
        "name": TLS_CONTAINER,
        "image": TLS_CLIENT_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "command": ["sh", "-c"],
        "args": [f"while true; do {TLS_VERIFY_COMMAND}; sleep 5; done"],
        "readinessProbe": {
            "exec": {"command": ["sh", "-c", TLS_VERIFY_COMMAND]},
            "timeoutSeconds": 10,
            "periodSeconds": 10,
            "failureThreshold": 1,
        },
        "volumeMounts": [
            {"name": "tls-ca", "mountPath": "/etc/tls-ca", "readOnly": True},
            {"name": "client-clock", "mountPath": "/etc/client-clock", "readOnly": True},
        ],
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "128Mi"},
        },
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }


def tls_client_volumes() -> list[dict]:
    return [
        {"name": "tls-ca", "configMap": {"name": TLS_CA}},
        {"name": "client-clock", "configMap": {"name": CLOCK_CONFIG}},
    ]


class TLSClockDriftHotelReservation(NodeClockDriftHotelReservation):
    task_version = "tls-validation-clock-v2"

    def __init__(self):
        super().__init__()
        self._tls_prepared = False
        self.root_cause = self.build_structured_root_cause(
            component="deployment/frontend outbound TLS client clock",
            namespace=self.namespace,
            description=(
                "The frontend's outbound TLS validation clock is advanced by 30 days through its client "
                "settings. A currently valid dependency certificate appears expired, so real TLS handshakes "
                "fail and the frontend loses Ready endpoints. Restore the client's clock configuration while "
                "preserving certificate, hostname and trust-chain verification. The physical node clock is healthy."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = TLSClockDriftMitigationOracle(self)

    def _generate_self_signed_cert(self) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as folder:
            cert, key = Path(folder) / "tls.crt", Path(folder) / "tls.key"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    f"/CN={TLS_HOSTNAME}",
                    "-addext",
                    f"subjectAltName=DNS:{TLS_HOSTNAME}",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return (
                base64.b64encode(cert.read_bytes()).decode(),
                base64.b64encode(key.read_bytes()).decode(),
                cert.read_text(),
            )

    def prepare_tls(self) -> None:
        """Create the healthy dependency before capturing the grading baseline."""
        if self._tls_prepared:
            return
        cert, key, pem = self._generate_self_signed_cert()
        self.core_v1.create_namespaced_secret(
            self.namespace,
            {
                "metadata": {"name": TLS_SECRET},
                "type": "kubernetes.io/tls",
                "data": {"tls.crt": cert, "tls.key": key},
            },
        )
        self.core_v1.create_namespaced_config_map(
            self.namespace,
            {
                "metadata": {"name": TLS_CA},
                "data": {"ca.crt": pem},
            },
        )
        self.core_v1.create_namespaced_config_map(
            self.namespace,
            {
                "metadata": {"name": CLOCK_CONFIG},
                "data": {CLOCK_KEY: "0"},
            },
        )
        self.core_v1.create_namespaced_service(
            self.namespace,
            {
                "metadata": {"name": TLS_SERVICE},
                "spec": {"selector": {"app": TLS_SERVICE}, "ports": [{"port": 9443, "targetPort": 9443}]},
            },
        )
        self.kubectl.apps_v1_api.create_namespaced_deployment(
            self.namespace,
            {
                "metadata": {"name": TLS_SERVICE},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": TLS_SERVICE}},
                    "template": {
                        "metadata": {"labels": {"app": TLS_SERVICE}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": "server",
                                    "image": TLS_CLIENT_IMAGE,
                                    "command": [
                                        "openssl",
                                        "s_server",
                                        "-accept",
                                        "9443",
                                        "-cert",
                                        "/etc/tls/tls.crt",
                                        "-key",
                                        "/etc/tls/tls.key",
                                        "-www",
                                        "-quiet",
                                    ],
                                    "ports": [{"containerPort": 9443}],
                                    "readinessProbe": {"tcpSocket": {"port": 9443}},
                                    "volumeMounts": [{"name": "tls", "mountPath": "/etc/tls", "readOnly": True}],
                                    "resources": {
                                        "requests": {"memory": "32Mi", "cpu": "10m"},
                                        "limits": {"memory": "128Mi", "cpu": "100m"},
                                    },
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                }
                            ],
                            "volumes": [{"name": "tls", "secret": {"secretName": TLS_SECRET}}],
                        },
                    },
                },
            },
        )
        self.kubectl.patch_deployment(
            "frontend",
            self.namespace,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [tls_client_container()],
                            "volumes": tls_client_volumes(),
                        }
                    }
                },
            },
        )
        self._wait_tls_ready()
        self._tls_prepared = True

    def _wait_tls_ready(self) -> None:
        # A previous failed delete must be retried before completed probe pods
        # can affect namespace readiness during preparation or recovery.
        self.mitigation_oracle._cleanup_tls_probes()
        # Deployment rollout status checks the current observed generation and
        # updated replicas, unlike a list of old already-ready pods.
        for name in (TLS_SERVICE, "frontend"):
            self.kubectl.exec_command_checked(
                f"kubectl rollout status deployment/{name} -n {self.namespace} --timeout=180s",
                timeout=190,
            )
        self.kubectl.wait_for_ready(self.namespace)
        pem = self.core_v1.read_namespaced_config_map(TLS_CA, self.namespace).data["ca.crt"]
        if not self.mitigation_oracle._fresh_tls_handshake(pem, "0"):
            raise RuntimeError("Healthy TLS preparation did not complete a verified handshake")

    @mark_fault_injected
    def inject_fault(self):
        self.prepare_tls()
        # A lost PATCH response can still mean the mutation was applied.
        self.fault_injected = True
        try:
            self.core_v1.patch_namespaced_config_map(
                CLOCK_CONFIG, self.namespace, {"data": {CLOCK_KEY: str(self.clock_drift_seconds)}}
            )
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                pods = self.core_v1.list_namespaced_pod(
                    self.namespace, label_selector="io.kompose.service=frontend"
                ).items
                for pod in pods:
                    states = pod.status.container_statuses or []
                    if any(state.name == TLS_CONTAINER and not state.ready for state in states):
                        logs = self.core_v1.read_namespaced_pod_log(
                            pod.metadata.name, self.namespace, container=TLS_CONTAINER, tail_lines=30
                        )
                        if "certificate has expired" in logs:
                            return
                time.sleep(3)
            raise RuntimeError("The TLS clock fault did not produce a real certificate-expiry failure")
        except Exception:
            self.recover_fault()
            raise

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        # Application cleanup owns these resources. Recovery only restores the
        # client clock; missing configuration cannot establish recovery.
        self.core_v1.patch_namespaced_config_map(CLOCK_CONFIG, self.namespace, {"data": {CLOCK_KEY: "0"}})
        self._wait_tls_ready()
