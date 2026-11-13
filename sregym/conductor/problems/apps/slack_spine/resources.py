"""Slack Spine: resources."""

from __future__ import annotations

import json
import time
from pathlib import Path

from sregym.conductor.oracles.cpu_throttling_mitigation import CpuThrottlingMitigationOracle
from sregym.conductor.oracles.imbalance_mitigation import ImbalanceMitigationOracle
from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
    KubeletEvictionThresholdMisconfigMitigationOracle,
)
from sregym.conductor.problems.apps.slack_spine.manifests import _SURGE_CLIENT
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port, make_app, reopen_connections
from sregym.conductor.problems.kubelet_eviction_threshold_misconfig import KubeletEvictionThresholdMisconfig
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.images import WORKLOAD_IMBALANCE_PROXY_IMAGE
from sregym.utils.decorators import mark_fault_injected


class CpuThrottlingSlack(Problem):
    """svc-message gets a CPU limit just above its observed peak; other svc-* roles get looser decoy limits.

    svc-message serves most of the load generator's sessions (history reads,
    posts); svc-workspace, the plan's first choice, is off the default request
    path (svc-channel only calls it with WORKSPACE_POLICY_CHECK=1).
    """

    CPU_LIMIT_DECOYS = [
        "svc-auth",
        "svc-channel",
        "svc-thread",
        "svc-search",
        "svc-notification",
        "svc-platform",
        "svc-workspace",
    ]
    SERVICE_LABEL_KEY = "app.kubernetes.io/component"
    # The load generator's arrival rate cycles over ~2 minutes, so usage is
    # sampled across a whole cycle (the original's 35s can land in a trough and
    # set a limit that starves the pod). svc-message's Node event loop is
    # bursty: at the original 1.15x over its peak its throttle rate hovered
    # around the injector's 5% floor, so the limit sits at the peak. The near
    # idle decoys throttle >10% at 2x, so they get 3x.
    CALIBRATION = {"n_samples": 12, "sample_window_seconds": 10, "tight_headroom": 1.0}
    LOOSE_HEADROOM = 3.0

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        configure_port(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{faulty_service}` deployment (the message API: channel history reads, posts, edits) "
                "has a CPU limit (and equal request) set at its measured steady-state peak usage, too "
                "low for its request bursts. The Linux CFS scheduler throttles the container whenever it exhausts "
                "its quota within a 100ms period, delaying bursts and causing tail latency and timeouts on the "
                "request paths that call it. The other svc-* roles were also given CPU limits, with ample headroom, "
                "so a CPU limit by itself is not the anomaly. `kubectl top pods` shows usage below the limit; the "
                "throttling is visible in the container's cgroup `cpu.stat` (high `nr_throttled`) and in "
                "`container_cpu_cfs_throttled_periods_total`. The fix is to raise the CPU limit to accommodate bursts, or remove it."
            ),
            oracle_factory=lambda problem: CpuThrottlingMitigationOracle(
                problem=problem, faulty_service=problem.faulty_service
            ),
        )

    def _injector(self) -> VirtualizationFaultInjector:
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.service_label_key = self.SERVICE_LABEL_KEY
        return injector

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = self._injector()
        injected = {}
        try:
            injected = injector.inject_cpu_throttle(
                microservices=[self.faulty_service],
                additional_services=self.CPU_LIMIT_DECOYS,
                loose_headroom_factor=self.LOOSE_HEADROOM,
                calibration_kwargs=dict(self.CALIBRATION),
            )
            self._patched_services = list(injected)
            injected = injector.verify_injection(injected=injected, faulty_services=[self.faulty_service])
        except Exception:
            if injected:
                print("CPU-throttling verification failed; restoring original resources")
                injector.recover_cpu_throttle(microservices=list(injected))
            raise
        self.injected_cpu_limit = injected.get(self.faulty_service)
        self.mitigation_oracle.oracles["fault"].injected_cpu_limit = self.injected_cpu_limit
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace} | limit {self.injected_cpu_limit}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        services = getattr(self, "_patched_services", [self.faulty_service, *self.CPU_LIMIT_DECOYS])
        self._injector().recover_cpu_throttle(microservices=services)


class KubeletEvictionThresholdMisconfigSlack(KubeletEvictionThresholdMisconfig):
    """A worker's kubelet evicts everything because its nodefs threshold is above the free space.

    svc-notification is pinned to that node with ``nodeName`` (bypassing the
    scheduler and the disk-pressure taint), so it is evicted and recreated in
    a loop. The node is chosen at injection time, once placement is known: never
    the load generator's node (its ledger is the health baseline), and the
    worker hosting the fewest pods with node-bound volumes (on kind every
    worker usually hosts one; those pods stay Pending until the node recovers).
    """

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-notification"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.target_node = "<chosen at injection>"
        self.injected_threshold: float | None = None
        self.injector = RemoteOSFaultInjector()
        configure_port(
            self,
            app_name,
            component=f"node/{self.target_node}",
            description=self._description(),
            oracle_factory=KubeletEvictionThresholdMisconfigMitigationOracle,
        )

    def _description(self) -> str:
        return (
            f"Node `{self.target_node}` reports `DiskPressure=True` because the `nodefs.available` hard-eviction "
            "threshold in its kubelet config (`/var/lib/kubelet/config.yaml`, `evictionHard`) was raised above the "
            "node's actual free-space percentage, not because the disk is full (`df` shows ample free space). The "
            "kubelet taints the node `node.kubernetes.io/disk-pressure` and evicts its pods; pods with node-bound "
            f"volumes on that node stay Pending. Deployment `{self.faulty_service}` (unread counts and notification "
            f"fan-out) is pinned to the node via `spec.template.spec.nodeName`, which bypasses the scheduler, so its "
            "pod is evicted and recreated in a continuous loop and the service is unavailable."
        )

    def _prepare_injection(self):
        super()._prepare_injection()
        self._state_path().write_text(self.target_node)

    def _state_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-eviction-node")

    @mark_fault_injected
    def recover_fault(self):
        if self.target_node.startswith("<") and self._state_path().exists():
            self.target_node = self._state_path().read_text().strip()
        super().recover_fault()
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )


class WorkloadImbalanceSlack(Problem):
    """A kube-proxy build with a broken endpoint probability meets a scaled-out svc-message and a load surge.

    As in the original: kube-proxy's DaemonSet image is replaced with SREGym's
    patched build (``computeProbability`` returns a constant ~1.1%, so with N
    endpoints the last iptables rule receives ~95% of new connections),
    svc-message (the load generator's target) is scaled to 5 replicas, and the
    workload surges. The surge is an in-namespace client Deployment
    (``loadgen-surge``) hammering svc-message's history endpoint with fresh
    connections; Slack Spine's load generator profile is fixed at deploy time.
    """

    REPLICAS = 5
    SURGE_NAME = "loadgen-surge"
    SURGE_THREADS = 128

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = [faulty_service]
        configure_port(
            self,
            app_name,
            component=f"daemonset/kube-proxy + deployment/{faulty_service}",
            description=(
                "The kube-system `kube-proxy` DaemonSet runs a non-standard image "
                f"(`{WORKLOAD_IMBALANCE_PROXY_IMAGE.split('@')[0]}`) whose iptables rule generation is buggy: "
                "every per-endpoint `statistic --mode random --probability` rule uses the same ~0.0115 "
                "probability instead of 1/n, so nearly all new connections fall through to the last endpoint of "
                f"each Service. Deployment `{faulty_service}` was scaled to {self.REPLICAS} replicas and the "
                "workload surged (an extra `loadgen-surge` client), but one pod receives almost all new "
                "connections and is overloaded (requests to it queue for seconds) while the other replicas stay "
                "nearly idle, degrading latency for end users. Mitigation: restore the stock kube-proxy image (the DaemonSet's original "
                "`registry.k8s.io/kube-proxy` tag) so traffic is balanced across the replicas."
            ),
            oracle_factory=ImbalanceMitigationOracle,
        )
        self.injector = VirtualizationFaultInjector(namespace="kube-system")
        self.injector_for_scale = VirtualizationFaultInjector(namespace=self.namespace)

    def _surge_manifest(self) -> dict:
        toolbox = self.kubectl.get_deployment("ops-toolbox", self.namespace)
        image = toolbox.spec.template.spec.containers[0].image
        labels = {"app.kubernetes.io/component": self.SURGE_NAME}
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.SURGE_NAME, "namespace": self.namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "surge",
                                "image": image,
                                "command": ["python3", "-c", _SURGE_CLIENT],
                                "env": [
                                    {"name": "SURGE_TARGET", "value": f"http://{self.faulty_service[0]}:8000"},
                                    {"name": "SURGE_THREADS", "value": str(self.SURGE_THREADS)},
                                ],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "64Mi"},
                                    "limits": {"cpu": "2", "memory": "256Mi"},
                                },
                            }
                        ],
                    },
                },
            },
        }

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_daemon_set_image_replacement(
            daemon_set_name="kube-proxy", new_image=WORKLOAD_IMBALANCE_PROXY_IMAGE
        )
        self.injector_for_scale.scale_pods_to(replicas=self.REPLICAS, microservices=self.faulty_service)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service[0]} -n {self.namespace} --timeout=300s",
            timeout=330,
        )
        # The load generator keeps its connections alive; replace the pods so it
        # re-dials through the new proxy rules (as the original's fresh workload did).
        reopen_connections(self, self.faulty_service[0])
        print("== Surge the workload ==")
        self.kubectl.exec_command_checked(f"kubectl apply -f - <<'EOF'\n{json.dumps(self._surge_manifest())}\nEOF")
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.SURGE_NAME} -n {self.namespace} --timeout=300s", timeout=330
        )
        time.sleep(30)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        if not self.injector.recover_daemon_set_image_replacement(daemon_set_name="kube-proxy"):
            raise RuntimeError("The saved kube-proxy image is missing")
        self.kubectl.exec_command(f"kubectl delete deployment {self.SURGE_NAME} -n {self.namespace} --ignore-not-found")
        self.injector_for_scale.scale_pods_to(replicas=1, microservices=self.faulty_service)
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service[0]} -n {self.namespace} --timeout=300s"
        )
