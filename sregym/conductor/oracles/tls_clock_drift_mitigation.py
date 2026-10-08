"""Verify clock recovery while retaining the actual outbound TLS contract."""

import contextlib
import copy
import os
import re
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.service.agent_visibility_policy import VERIFIER_PROBE_NAMESPACE


def _contains_expected(actual, expected) -> bool:
    """Compare required fields while tolerating API-populated defaults."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            _contains_expected(actual.get(key), value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_contains_expected(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return actual == expected


def _without_automatic_token_mount(container: dict | None, volumes: list[dict]) -> dict | None:
    """Kubernetes adds its service-account projection to live pod containers."""
    if container is None:
        return None
    projections = {
        volume["name"]
        for volume in volumes
        if volume["name"].startswith("kube-api-access-")
        and any("serviceAccountToken" in source for source in volume.get("projected", {}).get("sources", []))
    }
    result = copy.deepcopy(container)
    result["volumeMounts"] = [
        mount
        for mount in result.get("volumeMounts", [])
        if not (
            mount["name"] in projections
            and mount.get("readOnly") is True
            and mount.get("mountPath") == "/var/run/secrets/kubernetes.io/serviceaccount"
        )
    ]
    return result


class TLSClockDriftMitigationOracle(MitigationOracle):
    FAILURE_CLASSES = {
        "tls_identity_or_trust_changed": FailureClass.AGENT_ERROR,
        "tls_validation_contract_changed": FailureClass.AGENT_ERROR,
        "required_tls_resource_missing": FailureClass.AGENT_ERROR,
        "client_clock_unreadable": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.expected_secret = None
        self.expected_ca = None

    def capture_baseline(self) -> None:
        from sregym.conductor.problems.tls_clock_drift import TLS_CA, TLS_SECRET

        self.problem.prepare_tls()
        super().capture_baseline()
        self.expected_secret = copy.deepcopy(
            self.problem.core_v1.read_namespaced_secret(TLS_SECRET, self.problem.namespace).data
        )
        self.expected_ca = copy.deepcopy(
            self.problem.core_v1.read_namespaced_config_map(TLS_CA, self.problem.namespace).data
        )

    def evaluate(self) -> dict:
        from sregym.conductor.problems.tls_clock_drift import CLOCK_CONFIG, CLOCK_KEY, TLS_CA, TLS_SECRET, TLS_SERVICE

        if not self.replica_count or self.expected_secret is None or self.expected_ca is None:
            return self.fail_from_exception(RuntimeError("TLS clock baseline was not captured"))
        try:
            core = self.problem.core_v1
            secret = core.read_namespaced_secret(TLS_SECRET, self.problem.namespace)
            ca = core.read_namespaced_config_map(TLS_CA, self.problem.namespace)
            if secret.data != self.expected_secret or ca.data != self.expected_ca:
                return self.fail("tls_identity_or_trust_changed")
            settings = core.read_namespaced_config_map(CLOCK_CONFIG, self.problem.namespace).data or {}
            offset = settings.get(CLOCK_KEY, "")
            if not isinstance(offset, str) or not re.fullmatch(r"[+-]?[0-9]{1,10}", offset):
                return self.fail("client_clock_unreadable")
            if abs(int(offset)) > 60:
                return self.fail("fault_still_present", clock_skew_seconds=int(offset))
            if not self._contract_preserved():
                return self.fail("tls_validation_contract_changed")
            health = super().evaluate()
            if health.get("success") is not True:
                return health
            if not self._fresh_tls_handshake(ca.data["ca.crt"], offset):
                return self.fail("tls_handshake_failed", dependency=TLS_SERVICE)
            return {
                "success": True,
                "clock_skew_seconds": int(offset),
                "tls_handshake": True,
                "task_version": self.problem.task_version,
            }
        except ApiException as exc:
            if exc.status == 404:
                return self.fail("required_tls_resource_missing")
            return self.fail_from_exception(exc)
        except Exception as exc:
            return self.fail_from_exception(exc)

    def _contract_preserved(self) -> bool:
        from sregym.conductor.problems.tls_clock_drift import (
            TLS_CONTAINER,
            TLS_SECRET,
            TLS_SERVICE,
            tls_client_container,
            tls_client_volumes,
        )

        api = self.problem.kubectl.apps_v1_api
        serialize = client.ApiClient().sanitize_for_serialization
        frontend = serialize(api.read_namespaced_deployment("frontend", self.problem.namespace))
        spec = frontend["spec"]["template"]["spec"]
        container = next((c for c in spec.get("containers", []) if c["name"] == TLS_CONTAINER), None)
        if (
            not _contains_expected(container, tls_client_container())
            or container.get("env")
            or container.get("envFrom")
        ):
            return False
        volumes = {v["name"]: v for v in spec.get("volumes", [])}
        if not all(_contains_expected(volumes.get(v["name"]), v) for v in tls_client_volumes()):
            return False
        # A rollout must preserve the validation contract in every live replica,
        # not just a desired template while an altered old pod still serves.
        pods = self.problem.core_v1.list_namespaced_pod(
            self.problem.namespace, label_selector="io.kompose.service=frontend"
        ).items
        for pod in pods:
            if pod.metadata.deletion_timestamp is not None:
                continue
            actual = serialize(pod.spec)
            active = next((c for c in actual.get("containers", []) if c["name"] == TLS_CONTAINER), None)
            active = _without_automatic_token_mount(active, actual.get("volumes", []))
            if not _contains_expected(active, tls_client_container()) or active.get("env") or active.get("envFrom"):
                return False
        server = serialize(api.read_namespaced_deployment(TLS_SERVICE, self.problem.namespace))["spec"]["template"][
            "spec"
        ]
        service = serialize(self.problem.core_v1.read_namespaced_service(TLS_SERVICE, self.problem.namespace))["spec"]
        if service.get("selector") != {"app": TLS_SERVICE} or service.get("externalName"):
            return False
        if not any(port.get("port") == 9443 and port.get("targetPort") == 9443 for port in service.get("ports", [])):
            return False
        expected_server = {
            "name": "server",
            "image": tls_client_container()["image"],
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
            "volumeMounts": [{"name": "tls", "mountPath": "/etc/tls", "readOnly": True}],
        }
        serving = next((c for c in server.get("containers", []) if c["name"] == "server"), None)
        return (
            _contains_expected(serving, expected_server)
            and not serving.get("args")
            and not serving.get("env")
            and not serving.get("envFrom")
            and _contains_expected(
                next((v for v in server.get("volumes", []) if v["name"] == "tls"), None),
                {"name": "tls", "secret": {"secretName": TLS_SECRET}},
            )
        )

    def _fresh_tls_handshake(self, ca_pem: str, offset: str) -> bool:
        from sregym.conductor.problems.tls_clock_drift import TLS_SERVICE, TLS_VERIFY_COMMAND
        from sregym.service.runtime_images import TLS_CLIENT_IMAGE

        namespace = (
            VERIFIER_PROBE_NAMESPACE if os.environ.get("SREGYM_VERIFIER_CONTAINER") == "1" else self.problem.namespace
        )
        if namespace == VERIFIER_PROBE_NAMESPACE:
            self.problem.kubectl.exec_command_checked(
                f"kubectl create namespace {namespace} --dry-run=client -o yaml | kubectl apply -f -"
            )
        name = f"tls-client-check-{time.time_ns()}"
        command = TLS_VERIFY_COMMAND.replace(
            f"{TLS_SERVICE}:9443", f"{TLS_SERVICE}.{self.problem.namespace}.svc.cluster.local:9443"
        )
        command = (
            "mkdir -p /tmp/tls-ca /tmp/client-clock && "
            'printf "%s" "$TLS_CA_PEM" > /tmp/tls-ca/ca.crt && '
            'printf "%s" "$CLOCK_OFFSET" > /tmp/client-clock/clock-offset-seconds && '
            + command.replace("/etc/tls-ca", "/tmp/tls-ca").replace("/etc/client-clock", "/tmp/client-clock")
        )
        # Only current public application inputs go into this pod. No private
        # baseline, expected verdict or TLS private key is sent to the workload.
        pod = {
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "containers": [
                    {
                        "name": "client",
                        "image": TLS_CLIENT_IMAGE,
                        "command": ["sh", "-c", command],
                        "env": [{"name": "TLS_CA_PEM", "value": ca_pem}, {"name": "CLOCK_OFFSET", "value": offset}],
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}},
                    }
                ],
            },
        }
        core = self.problem.core_v1
        try:
            core.create_namespaced_pod(namespace, pod)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                current = core.read_namespaced_pod(name, namespace)
                if current.status.phase in {"Succeeded", "Failed"}:
                    return current.status.phase == "Succeeded"
                time.sleep(1)
            return False
        finally:
            with contextlib.suppress(ApiException):
                core.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
