"""Node clock drift causing TLS handshake failures on Hotel Reservation.
RFC: A worker node's system clock is significantly advanced (by disabling NTP and manually
advancing the clock). All pods on that node fail outbound TLS connections with
'certificate has expired or is not yet valid', even though the certificates are valid.
Recovery requires restoring the node's clock to cluster time and re-enabling NTP.
A short-lived TLS certificate (1-day validity) is deployed alongside workload,
sidecar on frontend pod validates the cert every 30 seconds. After node clock drift, cert
seems expired, x509 errors appear in sidecar log (to attempt to stimulate TLS handshake
failures.)
NOTE: Time-sync daemon names vary across environments (systemd-timesyncd, chrony,
ntpd, ntp, etc). Rather than guessing a fixed list of names, _advance_node_clock
dynamically discovers whichever time-sync service is actually running via
`systemctl list-units` and stops/masks it, so this fault works correctly regardless
of which daemon a given environment uses. recover_fault discovers and restores the
same way, plus steps the clock back, so the fault is fully reversible.
"""

import base64
import contextlib
import subprocess
import tempfile
import time
from pathlib import Path

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.node_clock_drift_mitigation import NodeClockDriftMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Affinity rule shared by the injector and restore pods to hard-prevent
# scheduling on control-plane nodes, regardless of taints.

_WORKER_ONLY_AFFINITY = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [
                {
                    "matchExpressions": [
                        {
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "DoesNotExist",
                        },
                        {
                            "key": "node-role.kubernetes.io/master",  # pre-1.24 clusters
                            "operator": "DoesNotExist",
                        },
                    ]
                }
            ]
        }
    }
}
_NODE_PROBE_LABEL = "node-probe"


class NodeClockDriftHotelReservation(Problem):
    """Inject node clock drift causing TLS validation failures."""

    clock_drift_seconds = 86400 * 30
    clock_injector_namespace = "default"
    clock_injector_image = "ubuntu:22.04"

    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.core_v1 = client.CoreV1Api()
        self.root_cause = self.build_structured_root_cause(
            component="node/system-clock",
            namespace="kube-system",
            description=(
                "A worker node's system clock is significantly skewed forward and its active time-sync "
                "service has been stopped and masked. "
                "All pods on this node fail outbound TLS connections with 'certificate has expired or is not yet valid' "
                "because the node's skewed clock makes even valid certificates APPEAR expired. The certificates are "
                "healthy; the issue is the node's clock synchronization. Recovery requires restoring the time-sync "
                "service and synchronizing the node's clock back to cluster time."
            ),
        )
        self.target_node = None
        self.injector_pod_name = None
        self.stopped_services = []
        self.app.create_workload()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = NodeClockDriftMitigationOracle(self)

    def requires_khaos(self) -> bool:
        return False

    # ── Node helpers ────────────────────────────────────────────────────────────
    def _is_control_plane_node(self, node_name: str) -> bool:
        """Return True if the named node carries a control-plane role label."""
        try:
            node = self.core_v1.read_node(node_name)
            labels = node.metadata.labels or {}
            return (
                "node-role.kubernetes.io/control-plane" in labels
                or "node-role.kubernetes.io/master" in labels  # pre-1.24 clusters
            )
        except ApiException:
            return False

    # ── TLS Infrastructure ──────────────────────────────────────────────────────
    def _generate_self_signed_cert(self) -> tuple[str, str, str]:
        """Generate a self-signed TLS certificate valid for only 1 day.
        A 1-day cert expires well within the 30-day clock drift window, guaranteeing
        x509 validation failures on the affected node without needing to push drift
        past the kube-apiserver cert expiry (for simulation)
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            key_file = tmpdir / "tls.key"
            cert_file = tmpdir / "tls.crt"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(key_file), "2048"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-new",
                    "-nodes",
                    "-key",
                    str(key_file),
                    "-sha256",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=hotel-reservation.local",
                    "-out",
                    str(cert_file),
                ],
                check=True,
                capture_output=True,
            )
            cert_pem = cert_file.read_text()
            cert_b64 = base64.b64encode(cert_file.read_bytes()).decode()
            key_b64 = base64.b64encode(key_file.read_bytes()).decode()
            return cert_b64, key_b64, cert_pem

    def _setup_tls_infrastructure(self) -> None:
        """Create TLS Secret, CA ConfigMap, and sidecar for the frontend.
        The self-signed cert is stored in two places:
          - kubernetes.io/tls Secret; represents a deployed TLS credential
          - plain ConfigMap; mounted into the sidecar as the CA bundle
            so openssl can validate the cert against the node's clock
        The cert validation is done directly by the sidecar,
        simulating what a service would do when establishing an outbound TLS connection.
        """
        print("[TLS] Generating self-signed certificate (1-day validity)")
        cert_b64, key_b64, cert_pem = self._generate_self_signed_cert()
        tls_secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "hotel-frontend-tls",
                "namespace": self.namespace,
            },
            "type": "kubernetes.io/tls",
            "data": {
                "tls.crt": cert_b64,
                "tls.key": key_b64,
            },
        }
        try:
            self.core_v1.create_namespaced_secret(self.namespace, tls_secret)
            print("[TLS] Created TLS secret: hotel-frontend-tls")
        except ApiException as e:
            if e.status == 409:
                print("[TLS] TLS secret already exists")
            else:
                raise
        ca_configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "hotel-frontend-ca",
                "namespace": self.namespace,
            },
            "data": {
                "ca.crt": cert_pem,
            },
        }
        try:
            self.core_v1.create_namespaced_config_map(self.namespace, ca_configmap)
            print("[TLS] Created CA ConfigMap: hotel-frontend-ca")
        except ApiException as e:
            if e.status == 409:
                print("[TLS] CA ConfigMap already exists")
            else:
                raise
        self._add_tls_health_check_sidecar()

    def _add_tls_health_check_sidecar(self) -> None:
        """
        Validates the short-lived cert against the node clock every 30 seconds.
        Once the node clock is skewed 30 days forward, openssl verify produces
        x509 certificate expired errors.
        A readiness probe checks for /tmp/sidecar-ready, which is only touched
        AFTER `apt-get install openssl` finishes. Without this, Kubernetes marks
        the container "Ready" the instant the process starts, before openssl is
        actually installed — _wait_for_sidecar_rollout() would then proceed to
        drift the clock while apt-get is still mid-download, causing apt's own
        HTTPS connection to the package mirror to start failing once the clock
        skews (same x509 error class as the fault itself), stalling readiness
        for several minutes before the sidecar loop ever starts.
        """
        sidecar_cmd = (
            "apt-get update -qq && apt-get install -y -qq openssl && "
            "touch /tmp/sidecar-ready && "
            "while true; do "
            "  openssl verify -verbose -CAfile /etc/tls-ca/ca.crt /etc/tls-ca/ca.crt; "
            "  sleep 30; "
            "done"
        )
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "affinity": _WORKER_ONLY_AFFINITY,
                        "volumes": [
                            {
                                "name": "tls-ca",
                                "configMap": {"name": "hotel-frontend-ca"},
                            }
                        ],
                        "containers": [
                            {
                                "name": "tls-health-check",
                                "image": "ubuntu:22.04",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["sh", "-c"],
                                "args": [sidecar_cmd],
                                "readinessProbe": {
                                    "exec": {"command": ["test", "-f", "/tmp/sidecar-ready"]},
                                    "initialDelaySeconds": 2,
                                    "periodSeconds": 3,
                                },
                                "volumeMounts": [
                                    {
                                        "name": "tls-ca",
                                        "mountPath": "/etc/tls-ca",
                                        "readOnly": True,
                                    }
                                ],
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "128Mi"},
                                    "limits": {"cpu": "100m", "memory": "512Mi"},
                                },
                            }
                        ],
                    }
                }
            }
        }
        try:
            self.kubectl.patch_deployment("frontend", self.namespace, patch_body)
            print("[TLS] Added TLS verification sidecar to frontend deployment")
        except Exception as e:
            print(f"[TLS] Warning: Could not patch frontend deployment with sidecar: {e}")

    def _wait_for_sidecar_rollout(self, timeout: int = 600) -> None:
        """Wait until a Running frontend pod exists with the tls-health-check sidecar
        fully ready, on a worker node.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pods = self.core_v1.list_namespaced_pod(
                    self.namespace, label_selector="io.kompose.service=frontend"
                ).items
            except Exception:
                pods = self.core_v1.list_namespaced_pod(self.namespace).items
            for pod in pods:
                container_names = [c.name for c in pod.spec.containers]
                if (
                    pod.status.phase == "Running"
                    and pod.spec.node_name
                    and "tls-health-check" in container_names
                    and pod.status.container_statuses
                    and all(cs.ready for cs in pod.status.container_statuses)
                    and not self._is_control_plane_node(pod.spec.node_name)
                ):
                    return
            time.sleep(3)
        raise RuntimeError(
            f"Timed out after {timeout}s waiting for a Running frontend pod "
            f"with the tls-health-check sidecar to appear on a worker node."
        )

    # ── Fault Injection ─────────────────────────────────────────────────────────
    @mark_fault_injected
    def inject_fault(self):
        print("Fault Injection (Node Clock Drift)")
        self._setup_tls_infrastructure()
        print("TLS infrastructure set up (Secret + CA ConfigMap + verification sidecar)")
        # Wait for the sidecar rollout to fully land (openssl installed) before
        # picking a target node, otherwise might grab the OLD pod's node instead
        # of the NEW one's, or drift the clock before openssl is ready
        self._wait_for_sidecar_rollout()
        print("Sidecar rollout confirmed Running and ready")
        self.target_node = self._select_target_node()
        print(f"Target node: {self.target_node}")
        self._advance_node_clock(self.target_node)
        print(f"Advanced system clock on {self.target_node} by {self.clock_drift_seconds}s (30 days)")
        time.sleep(10)
        print("Node clock skewed. x509 certificate errors will now appear in sidecar log")

    @mark_fault_injected
    def recover_fault(self):
        """Fully reverse the fault: restore the time-sync service(s) that were
        stopped/masked, step clock back, clean up injector pods
        Must run BEFORE cleanup, since the restore pod also needs to land on the
        same target node via nsenter.
        """
        print("Fault Recovery (Node Clock Drift)")
        if self.target_node:
            self._restore_node_clock(self.target_node)
        else:
            print("No target_node recorded; skipping clock/service restore")
        self._cleanup_injector_pods()
        print("Cleaned up clock drift injector pods")

    # ── Node Targeting ──────────────────────────────────────────────────────────
    def _select_target_node(self) -> str:
        """Select the node running the frontend pod that has the TLS sidecar"""
        try:
            pods = self.core_v1.list_namespaced_pod(self.namespace, label_selector="io.kompose.service=frontend").items
        except Exception:
            pods = self.core_v1.list_namespaced_pod(self.namespace).items
        for pod in pods:
            container_names = [c.name for c in pod.spec.containers]
            if (
                pod.status.phase == "Running"
                and pod.spec.node_name
                and "tls-health-check" in container_names
                and not self._is_control_plane_node(pod.spec.node_name)
            ):
                return pod.spec.node_name
        raise RuntimeError(
            f"No running frontend pod with tls-health-check sidecar found on a "
            f"worker node in namespace '{self.namespace}'"
        )

    def _advance_node_clock(self, node: str) -> None:
        """Create a privileged pod that discovers + disables the active time-sync
        service, advances the clock
        The pod carries a nodeAffinity rule that hard-prevents scheduling on
        control-plane nodes as a second layer of defence beyond _select_target_node.
        (shouldn't need as long as control node is tainted)
        """
        advance_cmd = f"""
            set -e
            echo "Discovering active time-sync service..."
            TIME_SYNC_SERVICES=$(nsenter --target 1 --mount --uts --ipc --net --pid -- \
                systemctl list-units --type=service --state=running --no-legend 2>/dev/null \
                | awk '{{print $1}}' \
                | grep -iE 'ntpd|ntp|chrony|timesync' || true)
            echo "DISCOVERED_SERVICES:$TIME_SYNC_SERVICES"
            if [ -n "$TIME_SYNC_SERVICES" ]; then
                echo "Found active time-sync service(s): $TIME_SYNC_SERVICES"
                for svc in $TIME_SYNC_SERVICES; do
                    echo "Stopping and masking: $svc"
                    nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl stop "$svc" || true
                    nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl mask "$svc" || true
                done
            else
                echo "No active time-sync service found via systemd; proceeding with clock jump only."
            fi
            echo "Advancing system clock by {self.clock_drift_seconds} seconds:"
            nsenter --target 1 --mount --uts --ipc --net --pid -- date
            nsenter --target 1 --mount --uts --ipc --net --pid -- \
                date -s "+{self.clock_drift_seconds} seconds"
            nsenter --target 1 --mount --uts --ipc --net --pid -- date
            echo "Clock drift injection complete. Keeping pod alive:"
            tail -f /dev/null
        """
        pod_name = f"node-probe-{int(time.time() * 1000)}"
        self.injector_pod_name = pod_name
        pod_spec = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self.clock_injector_namespace,
                "labels": {"app": _NODE_PROBE_LABEL},
            },
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": node},
                "affinity": _WORKER_ONLY_AFFINITY,
                "hostNetwork": True,
                "hostPID": True,
                "hostIPC": True,
                "terminationGracePeriodSeconds": 0,
                "automountServiceAccountToken": False,
                "containers": [
                    {
                        "name": "node-probe",
                        "image": self.clock_injector_image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c"],
                        "args": [advance_cmd],
                        "securityContext": {
                            "privileged": True,
                            "capabilities": {"add": ["SYS_TIME", "SYS_ADMIN"]},
                        },
                    }
                ],
            },
        }
        try:
            self.core_v1.create_namespaced_pod(self.clock_injector_namespace, pod_spec)
            print(f"Created node probe pod: {pod_name}")
            time.sleep(15)
            # Parse the injector's own logs to find out which service(s) it
            # actually stopped, so recover_fault restores exactly those.
            self.stopped_services = []
            try:
                logs = self.core_v1.read_namespaced_pod_log(pod_name, self.clock_injector_namespace)
                for line in logs.splitlines():
                    if line.startswith("DISCOVERED_SERVICES:"):
                        services_str = line.split(":", 1)[1].strip()
                        if services_str:
                            self.stopped_services = services_str.split()
                        break
            except ApiException as e:
                print(f"Warning: could not read injector logs to determine stopped services: {e}")
        except ApiException as e:
            print(f"Failed to create node probe pod: {e}")
            raise

    def _restore_node_clock(self, node: str) -> None:
        """Step the node's clock back to cluster time and restore the time-sync service.

        Idempotent: checks actual skew before stepping — if already within 60s
        (e.g. NTP self-corrected), the clock step is skipped to avoid new drift.

        If self.stopped_services is empty (injection crashed before log parsing),
        falls back to discovering currently masked time-sync services rather than
        skipping NTP restore entirely.
        """
        services = self.stopped_services
        control_plane_epoch = int(time.time())

        if services:
            service_restore_block = "\n".join(
                f'echo "Restoring: {svc}"\n'
                f"nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl unmask {svc} || true\n"
                f"nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl start {svc} || true"
                for svc in services
            )
        else:
            print(
                "Warning: no recorded stopped services — falling back to dynamic discovery of masked time-sync services."
            )
            service_restore_block = """
MASKED=$(nsenter --target 1 --mount --uts --ipc --net --pid -- \\
    systemctl list-unit-files --state=masked --no-legend 2>/dev/null \\
    | awk '{print $1}' \\
    | grep -iE 'ntpd|ntp|chrony|timesync' || true)

if [ -n "$MASKED" ]; then
    echo "Found masked time-sync service(s): $MASKED"
    for svc in $MASKED; do
        echo "Restoring: $svc"
        nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl unmask "$svc" || true
        nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl start "$svc" || true
    done
else
    echo "No masked time-sync services found; trying common daemon names as last resort..."
    for svc in systemd-timesyncd chrony chronyd ntp ntpd; do
        nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl unmask "$svc" 2>/dev/null || true
        nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl start "$svc" 2>/dev/null || true
    done
fi
"""

        restore_cmd = f"""
            set -e
            {service_restore_block}
            CURRENT_EPOCH=$(nsenter --target 1 --mount --uts --ipc --net --pid -- date +%s)
            SKEW=$((CURRENT_EPOCH - {control_plane_epoch}))
            ABS_SKEW=${{SKEW#-}}
            echo "Current node clock skew: ${{SKEW}}s (abs: ${{ABS_SKEW}}s)"
            if [ "$ABS_SKEW" -gt 60 ]; then
                echo "Skew exceeds 60s; stepping clock to control-plane time..."
                nsenter --target 1 --mount --uts --ipc --net --pid -- date -s "@{control_plane_epoch}"
            else
                echo "Clock already within 60s tolerance; skipping manual step."
            fi
            echo "Final node time:"
            nsenter --target 1 --mount --uts --ipc --net --pid -- date
            echo "Clock and time-sync service restoration complete."
        """
        pod_name = f"node-probe-{int(time.time() * 1000)}"
        pod_spec = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self.clock_injector_namespace,
                "labels": {"app": _NODE_PROBE_LABEL},
            },
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": node},
                "affinity": _WORKER_ONLY_AFFINITY,
                "hostNetwork": True,
                "hostPID": True,
                "hostIPC": True,
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 0,
                "automountServiceAccountToken": False,
                "containers": [
                    {
                        "name": "node-probe",
                        "image": self.clock_injector_image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sh", "-c"],
                        "args": [restore_cmd],
                        "securityContext": {
                            "privileged": True,
                            "capabilities": {"add": ["SYS_TIME", "SYS_ADMIN"]},
                        },
                    }
                ],
            },
        }
        try:
            self.core_v1.create_namespaced_pod(self.clock_injector_namespace, pod_spec)
            print(f"Created node restore probe: {pod_name}")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                pod = self.core_v1.read_namespaced_pod(pod_name, self.clock_injector_namespace)
                if pod.status.phase in ["Succeeded", "Failed"]:
                    break
                time.sleep(2)
            logs = self.core_v1.read_namespaced_pod_log(pod_name, self.clock_injector_namespace)
            print(logs)
        except ApiException as e:
            print(f"Failed to create/run restore probe: {e}")
        finally:
            with contextlib.suppress(ApiException):
                self.core_v1.delete_namespaced_pod(pod_name, self.clock_injector_namespace, grace_period_seconds=0)

    def _cleanup_injector_pods(self) -> None:
        """Delete injector + restore pods in case _restore_node_clock cleanup
        didn't finish (race condition or failure).
        """
        try:
            pods = self.core_v1.list_namespaced_pod(
                self.clock_injector_namespace, label_selector=f"app={_NODE_PROBE_LABEL}"
            ).items
            for pod in pods:
                with contextlib.suppress(ApiException):
                    self.core_v1.delete_namespaced_pod(
                        pod.metadata.name, self.clock_injector_namespace, grace_period_seconds=0
                    )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                pods = self.core_v1.list_namespaced_pod(
                    self.clock_injector_namespace, label_selector=f"app={_NODE_PROBE_LABEL}"
                ).items
                if not pods:
                    return
                time.sleep(1)
        except ApiException:
            pass
