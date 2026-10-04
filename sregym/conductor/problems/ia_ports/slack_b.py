"""SREGym problems ported to the Incident Arena apps (environment scaling)."""

from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.cpu_throttling_mitigation import CpuThrottlingMitigationOracle
from sregym.conductor.oracles.hpa_control_plane_mitigation import HPAControlPlaneMitigationOracle
from sregym.conductor.oracles.imbalance_mitigation import ImbalanceMitigationOracle
from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
    KubeletEvictionThresholdMisconfigMitigationOracle,
)
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.cpu_throttling import CpuThrottling
from sregym.conductor.problems.hpa_missing_effective_cpu_request import HPAMissingEffectiveCPURequest
from sregym.conductor.problems.kafka_queue_problems import KafkaQueueProblems
from sregym.conductor.problems.kubelet_eviction_threshold_misconfig import KubeletEvictionThresholdMisconfig
from sregym.conductor.problems.lite_ia.k8s import ported, reopen_connections
from sregym.conductor.problems.lite_ia.kafka import KafkaPoisonPillHOLBlockIA
from sregym.conductor.problems.persistent_volume_affinity_violation import PersistentVolumeAffinityViolation
from sregym.conductor.problems.pod_anti_affinity_deadlock import PodAntiAffinityDeadlock
from sregym.conductor.problems.resource_request import ResourceRequestTooLarge
from sregym.conductor.problems.taint_no_toleration import TaintNoToleration
from sregym.conductor.problems.workload_imbalance import WorkloadImbalance
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.images import WORKLOAD_IMBALANCE_PROXY_IMAGE
from sregym.utils.decorators import mark_fault_injected


def _selector(problem, deployment: str) -> str:
    """``k=v,...`` label selector of a Deployment's own pods (from ``spec.selector.matchLabels``)."""
    dep = problem.kubectl.get_deployment(deployment, problem.namespace)
    return ",".join(f"{k}={v}" for k, v in sorted(dep.spec.selector.match_labels.items()))


def _clean_manifest(deployment_yaml: dict) -> dict:
    """A Deployment manifest from ``kubectl get -o yaml`` that can be re-created after a delete."""
    dyaml = copy.deepcopy(deployment_yaml)
    meta = dyaml.get("metadata", {})
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
        meta.pop(key, None)
    (meta.get("annotations") or {}).pop("deployment.kubernetes.io/revision", None)
    dyaml.pop("status", None)
    return dyaml


class ResourceRequestTooLargeSlack(ResourceRequestTooLarge):
    """svc-channel requests twice the largest node's memory; its recreated pod never schedules.

    svc-channel resolves channel authorization for every message send, so
    posts fail while it is down (svc-search, the plan's first choice, carries
    under 1% of the load generator's sessions and the outage stayed invisible).
    """

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-channel"):
        self.faulty_service = faulty_service
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` (channel membership and authorization, called by svc-message on "
                "every message send) was re-created with a "
                "container memory request of about twice the memory capacity of the largest node, so the scheduler "
                "cannot place its pod anywhere. The pod stays Pending with `Insufficient memory` "
                "FailedScheduling events, the Deployment has no Ready replica and the Service no endpoints, so "
                "svc-message's channel authorization check fails and users' message posts are rejected."
            ),
            oracle_factory=MitigationOracle,
        )

    def set_memory_limit(self, deployment_yaml):
        """Request (and limit: Slack's roles set one, and a request may not exceed it) 2x the largest node."""
        dyaml = copy.deepcopy(deployment_yaml)
        capacity_ki = int(self.kubectl.get_node_memory_capacity())  # KubeCtl reports KiB
        new_value = f"{(capacity_ki * 2) // 1024}Mi"
        resources = dyaml["spec"]["template"]["spec"]["containers"][0].setdefault("resources", {})
        resources.setdefault("requests", {})["memory"] = new_value
        if "memory" in (resources.get("limits") or {}):
            resources["limits"]["memory"] = new_value
        print(f"Setting memory request to {new_value} for {self.faulty_service}")
        return dyaml


class PodAntiAffinityDeadlockSlack(PodAntiAffinityDeadlock):
    """svc-thread gets a required pod anti-affinity that no node can satisfy.

    The anti-affinity term selects the chart-wide label every Slack Spine pod
    carries (``app.kubernetes.io/name=slack-spine``) per hostname, so each
    worker already hosts a matching pod and every svc-thread replica is Pending
    ("didn't match pod anti-affinity rules"). As in the original, the
    Deployment is re-created with more replicas than nodes.
    """

    ANTI_AFFINITY_LABELS = {"app.kubernetes.io/name": "slack-spine"}

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-thread"):
        self.faulty_service = faulty_service
        self.original_replicas: int | None = None
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` was re-created with more replicas than the cluster has nodes and a "
                "strict `requiredDuringSchedulingIgnoredDuringExecution` pod anti-affinity on "
                "`topologyKey: kubernetes.io/hostname` whose labelSelector is the chart-wide label "
                "`app.kubernetes.io/name=slack-spine` (carried by every app pod) instead of the component's own "
                "label. Every node already runs a matching pod, so no node is eligible: all of its pods stay Pending "
                "with `didn't match pod anti-affinity rules` events, the Service has no endpoints, and thread "
                "requests (opening threads, posting replies) fail."
            ),
            oracle_factory=MitigationOracle,
        )
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

    def _redeploy(self, deployment_yaml: dict) -> None:
        path = self.injector._write_yaml_to_file(self.faulty_service, deployment_yaml)
        self.kubectl.exec_command(f"kubectl delete deployment {self.faulty_service} -n {self.namespace} --wait=true")
        self.kubectl.exec_command_checked(f"kubectl apply -f {path} -n {self.namespace}")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        dyaml = _clean_manifest(self.injector._get_deployment_yaml(self.faulty_service))
        self.original_replicas = dyaml["spec"].get("replicas", 1)
        node_count = len(self.kubectl.core_v1_api.list_node().items)
        dyaml["spec"]["replicas"] = node_count + 1
        affinity = dyaml["spec"]["template"]["spec"].setdefault("affinity", {})
        affinity["podAntiAffinity"] = {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {
                    "labelSelector": {"matchLabels": dict(self.ANTI_AFFINITY_LABELS)},
                    "topologyKey": "kubernetes.io/hostname",
                }
            ]
        }
        self._redeploy(dyaml)
        time.sleep(30)
        print(f"{self.faulty_service}: {node_count + 1} replicas with an unsatisfiable required anti-affinity")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        dyaml = _clean_manifest(self.injector._get_deployment_yaml(self.faulty_service))
        pod_spec = dyaml["spec"]["template"]["spec"]
        (pod_spec.get("affinity") or {}).pop("podAntiAffinity", None)
        if not pod_spec.get("affinity"):
            pod_spec.pop("affinity", None)
        dyaml["spec"]["replicas"] = self.original_replicas or 1
        self._redeploy(dyaml)
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )


class TaintNoTolerationSlack(TaintNoToleration):
    """Every node is tainted and svc-thread only tolerates an unrelated key; its pods are rescheduled."""

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-thread"):
        self.faulty_service = faulty_service
        ported(
            self,
            app_name,
            component=(
                f"nodes (taint sre-fault=blocked:NoSchedule on all nodes) / deployment/{faulty_service} tolerations"
            ),
            description=(
                "Every cluster node is tainted `sre-fault=blocked:NoSchedule`, while Deployment "
                f"`{faulty_service}` only carries a non-matching toleration (`dummy-key`). Its pods were "
                "rescheduled after the taint, so the replacement pods stay Pending with "
                "`untolerated taint {sre-fault: blocked}` FailedScheduling events; the Service has no endpoints and "
                "thread requests (opening threads, posting replies) fail. Pods already running elsewhere are "
                "unaffected because NoSchedule does not evict. Naming either the node-wide taint or the "
                f"missing matching toleration on `{faulty_service}` is a correct localization."
            ),
            oracle_factory=MitigationOracle,
        )
        self.faulty_nodes = self._pick_all_nodes()
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

    @mark_fault_injected
    def inject_fault(self):
        print(f"Injecting Fault to Service {self.faulty_service} on Nodes {self.faulty_nodes}")
        for node in self.faulty_nodes:
            self.kubectl.exec_command_checked(f"kubectl taint node {node} sre-fault=blocked:NoSchedule --overwrite")
        patch = json.dumps(
            [
                {
                    "op": "add",
                    "path": "/spec/template/spec/tolerations",
                    "value": [{"key": "dummy-key", "operator": "Exists", "effect": "NoSchedule"}],
                }
            ]
        )
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=json -p='{patch}'"
        )
        # The original selects pods by ``app=``; Slack Spine labels them by component.
        self.kubectl.exec_command_checked(
            f"kubectl delete pod -l {_selector(self, self.faulty_service)} -n {self.namespace}"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("Fault Recovery")
        for node in self.faulty_nodes:
            self.kubectl.exec_command(f"kubectl taint node {node} sre-fault=blocked:NoSchedule-")
        self.kubectl.exec_command("kubectl delete pods --field-selector=status.phase=Pending --all-namespaces")
        patch = json.dumps([{"op": "remove", "path": "/spec/template/spec/tolerations"}])
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=json -p='{patch}'"
        )
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )


class PersistentVolumeAffinityViolationSlack(PersistentVolumeAffinityViolation):
    """svc-notification mounts a PV bound to one worker but is pinned to another."""

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-notification"):
        self.faulty_service = faulty_service
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` was re-created with a volume from PVC `temp-pvc`, bound to "
                "PersistentVolume `temp-pv` whose nodeAffinity requires one worker node, while the pod's "
                "`nodeSelector` (`kubernetes.io/hostname`) pins it to a different worker. The placement is "
                "unsatisfiable: the pod stays Pending with `volume node affinity conflict` scheduling events, the "
                "Service has no endpoints, and unread counts (`GET /unread`) and notification fan-out fail."
            ),
            oracle_factory=MitigationOracle,
        )


class HPAMissingEffectiveCPURequestSlack(HPAMissingEffectiveCPURequest):
    """A CPU-utilization HPA on svc-message whose pods lost their CPU request (and limit).

    As in the original, the pods stay Running/Ready: the fault breaks the
    autoscaling control loop, not the serving path, so the load generator is
    not expected to see errors while it is in place.
    """

    HPA_NAME = "svc-message-capacity"

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        ported(
            self,
            app_name,
            component=f"Deployment/{faulty_service} resource configuration and HorizontalPodAutoscaler/{self.HPA_NAME}",
            description=(
                f"The message-send autoscaling loop is broken: `Deployment/{faulty_service}` produces pods without "
                f"an effective CPU request (both `requests.cpu` and `limits.cpu` were removed from its container), "
                f"while `HorizontalPodAutoscaler/{self.HPA_NAME}` scales that deployment on CPU utilization "
                f"(target {self.HPA_CPU_TARGET_PERCENT}%, {self.HPA_MIN_REPLICAS}-{self.HPA_MAX_REPLICAS} replicas). "
                "Utilization is computed relative to the CPU request, so the HPA reports `<unknown>/60%`, "
                "`ScalingActive=False` and `FailedGetResourceMetric` (`missing request for cpu`) and can never scale "
                f"`{faulty_service}` out under load. The pods themselves remain Running/Ready. A valid mitigation "
                "restores a computable CPU metric, typically by restoring the CPU request; manually scaling the "
                "deployment without fixing the HPA is not sufficient."
            ),
            oracle_factory=lambda problem: HPAControlPlaneMitigationOracle(
                problem=problem, deployment_name=problem.faulty_service, hpa_name=problem.HPA_NAME
            ),
        )

    def _remove_effective_cpu_request(self, service: str):
        """Remove requests.cpu and limits.cpu with a JSON patch.

        The original re-applies an edited manifest without its last-applied
        annotation, which (on Helm-managed Deployments) merges and keeps the
        CPU fields.
        """
        deployment = self._get_deployment_json(service)
        ops = []
        for i, container in enumerate(deployment["spec"]["template"]["spec"]["containers"]):
            resources = container.get("resources") or {}
            for kind in ("requests", "limits"):
                if "cpu" in (resources.get(kind) or {}):
                    ops.append({"op": "remove", "path": f"/spec/template/spec/containers/{i}/resources/{kind}/cpu"})
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        annotations = deployment["spec"]["template"].get("metadata", {}).get("annotations")
        if annotations is None:
            ops.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": {}})
        ops.append(
            {
                "op": "add",
                "path": "/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt",
                "value": stamp,
            }
        )
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {service} -n {self.namespace} --type=json -p '{json.dumps(ops)}'"
        )
        print(f"Removed effective CPU request from Deployment/{service}")


class CpuThrottlingSlack(CpuThrottling):
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
        self.faulty_service = faulty_service
        ported(
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


class IndexLaneLagOracle(Oracle):
    """Slack Spine's search-index lane keeps up with its queue again.

    Passes when the ``index`` consumer group's total lag on ``jobs.index`` is
    back under ``max_lag`` and a fresh probe message sent through svc-message
    becomes searchable within ``freshness_s`` seconds (its partition is the one
    the backlog was built on).
    """

    FAILURE_CLASSES = {"lag_not_drained": "agent_error", "message_not_indexed": "agent_error"}

    def __init__(self, problem, max_lag: int = 25, freshness_s: float = 60):
        super().__init__(problem)
        self.max_lag = max_lag
        self.freshness_s = freshness_s

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Index lane lag ==")
        problem = self.problem
        try:
            lag = problem.total_lag()
            probe = problem.send_probes(1)[0]
            found = probe in problem.search([probe], must=[probe], wait_s=self.freshness_s)
            lag_after = problem.total_lag()
        except Exception as exc:
            print(f"❌ Could not observe the index lane: {exc}")
            return self.fail("message_not_indexed", error=str(exc))
        detail = {"lag": lag, "lag_after_probe": lag_after, "probe_searchable": found}
        if lag_after > self.max_lag:
            print(f"❌ {problem.CONSUMER_GROUP} lag on {problem.TOPIC} is {lag} -> {lag_after} (max {self.max_lag})")
            return self.fail("lag_not_drained", **detail)
        if not found:
            print(f"❌ probe message was not searchable within {self.freshness_s:.0f}s")
            return self.fail("message_not_indexed", **detail)
        print(f"✅ lag {lag_after} <= {self.max_lag}; probe searchable within {self.freshness_s:.0f}s")
        return {"success": True, **detail}


class KafkaQueueProblemsSlack(KafkaQueueProblems):
    """A queue overload meets a slowed consumer on Slack Spine's search-index lane.

    The original flips the OpenTelemetry demo's ``kafkaQueueProblems`` flag,
    which floods the Kafka topic and adds a consumer-side processing delay so
    consumer lag spikes. Here the ``worker-index`` lane (consumer group
    ``index`` on ``jobs.index``) gets its authored per-job cost raised
    (``HANDLER_MS``) and its concurrency dropped to one through its env, and a
    burst of messages is pushed through the real send path. The backlog keeps
    growing under the load generator's sends: messages are stored and delivered
    but become searchable only after many minutes. The load generator's search
    session only checks that ``/search`` answers, so the symptom is search
    freshness (and the lane's lag metric), not HTTP errors.
    """

    LANE = "index"
    TOPIC = "jobs.index"
    CONSUMER_GROUP = "index"
    CONSUMER_DEPLOYMENT = "worker-index"
    BROKER_SELECTOR = "app.kubernetes.io/component=redpanda"
    PROBE_PREFIX = "lagprobe"
    SLOW_ENV = {"HANDLER_MS": "4000", "LANE_CONCURRENCY": "1"}
    BURST = 300

    # Broker and toolbox helpers shared with the poison-pill port.
    _broker_pod = KafkaPoisonPillHOLBlockIA._broker_pod
    rpk = KafkaPoisonPillHOLBlockIA.rpk
    _toolbox_probe = KafkaPoisonPillHOLBlockIA._toolbox_probe
    _message = KafkaPoisonPillHOLBlockIA._message
    send_probes = KafkaPoisonPillHOLBlockIA.send_probes
    search = KafkaPoisonPillHOLBlockIA.search
    _rollout = KafkaPoisonPillHOLBlockIA._rollout

    def __init__(self, app_name: str = "slack_spine", channel_id: str = "chan-0"):
        self.channel_id = channel_id
        self.org_id = f"org-{channel_id}"
        self.faulty_service = self.CONSUMER_DEPLOYMENT
        self.feature_flag = None
        self.run_tag = uuid.uuid4().hex[:8]
        ported(
            self,
            app_name,
            component=f"deployment/{self.CONSUMER_DEPLOYMENT} (Redpanda topic {self.TOPIC}, consumer group {self.CONSUMER_GROUP})",
            description=(
                f"The `{self.TOPIC}` Redpanda queue is overloaded relative to its consumer. The "
                f"`{self.CONSUMER_DEPLOYMENT}` lane (consumer group `{self.CONSUMER_GROUP}`) had its per-job "
                f"processing cost raised to `HANDLER_MS={self.SLOW_ENV['HANDLER_MS']}` (default 8 ms) and its "
                f"parallelism cut to `LANE_CONCURRENCY={self.SLOW_ENV['LANE_CONCURRENCY']}` (default 4) through "
                "env vars on the deployment, so it drains well under one job per second, while a burst of "
                "message sends (plus the normal message traffic, enqueued by svc-message through kafkagate) "
                "filled the topic. Consumer lag (`kafka_consumergroup_lag`, `rpk group describe index`) keeps "
                "growing: new messages are stored and delivered but take many minutes to become searchable. Pods "
                "stay Running and Ready and no request fails. Mitigation: restore the lane's processing cost and "
                "concurrency (deployment env or the worker's `/admin/config`) so the backlog drains, without "
                "skipping (resetting offsets past) the queued messages."
            ),
            oracle_factory=IndexLaneLagOracle,
        )

    def total_lag(self) -> int:
        out = self.rpk(f"group describe {self.CONSUMER_GROUP}")
        for line in out.splitlines():
            if line.startswith("TOTAL-LAG"):
                return int(line.split()[1])
        raise RuntimeError(f"no TOTAL-LAG for group {self.CONSUMER_GROUP}: {out[-300:]!r}")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: slow index lane + queue burst ==")
        env = " ".join(f"{k}={v}" for k, v in self.SLOW_ENV.items())
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} {env}"
        )
        self._rollout()
        ids = [f"{self.PROBE_PREFIX}{self.run_tag}{uuid.uuid4().hex[:12]}" for _ in range(self.BURST)]
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            self._toolbox_probe({"post": [self._message(i, "bulk import") for i in chunk], "org_id": self.org_id})
        time.sleep(20)
        print(f"Index lane slowed ({env}); {self.BURST} messages queued; lag now {self.total_lag()}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: restore the index lane's cost and concurrency ==")
        env = " ".join(f"{k}-" for k in self.SLOW_ENV)
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.CONSUMER_DEPLOYMENT} -n {self.namespace} {env}"
        )
        self._rollout()


class KubeletEvictionThresholdMisconfigSlack(KubeletEvictionThresholdMisconfig):
    """A worker's kubelet evicts everything because its nodefs threshold is above the free space.

    svc-notification is pinned to that node with ``nodeName`` (bypassing the
    scheduler and the disk-pressure taint), so it is evicted and recreated in
    a loop. The node is chosen at injection time, once placement is known: never
    the load generator's node (its ledger is the health baseline), and the
    worker hosting the fewest pods with node-bound volumes (on kind every
    worker usually hosts one; those pods stay Pending until the node recovers).
    """

    LOADGEN_SELECTOR = "app.kubernetes.io/component=loadgen"

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-notification"):
        self.faulty_service = faulty_service
        self.target_node = "<chosen at injection>"
        self.injected_threshold: float | None = None
        self.injector = RemoteOSFaultInjector()
        ported(
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

    def _choose_node(self) -> str:
        pods = self.kubectl.core_v1_api.list_pod_for_all_namespaces().items
        workers = sorted(
            n.metadata.name
            for n in self.kubectl.core_v1_api.list_node().items
            if "node-role.kubernetes.io/control-plane" not in (n.metadata.labels or {})
        )
        loadgen_nodes = {
            p.spec.node_name
            for p in pods
            if p.metadata.namespace == self.namespace
            and (p.metadata.labels or {}).get("app.kubernetes.io/component") == "loadgen"
        }
        candidates = [w for w in workers if w not in loadgen_nodes] or workers

        def bound(node: str) -> int:
            return sum(
                1
                for p in pods
                if p.spec.node_name == node and any(v.persistent_volume_claim for v in (p.spec.volumes or []))
            )

        return min(candidates, key=lambda node: (bound(node), node))

    @mark_fault_injected
    def inject_fault(self):
        self.target_node = self._choose_node()
        self.root_cause = self.build_structured_root_cause(
            component=f"node/{self.target_node}", namespace=self.namespace, description=self._description()
        )
        self.diagnosis_oracle.expected = self.root_cause
        self._state_path().write_text(self.target_node)
        print("== Fault Injection ==")
        # The original patches nodeName in place; on a 1-replica Deployment the
        # rolling update keeps the old ReplicaSet's pod (rescheduled off the
        # pressured node) serving, so the Deployment is re-created pinned instead.
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        dyaml = _clean_manifest(injector._get_deployment_yaml(self.faulty_service))
        dyaml["spec"]["template"]["spec"]["nodeName"] = self.target_node
        path = injector._write_yaml_to_file(f"{self.faulty_service}-pinned", dyaml)
        self.kubectl.exec_command(f"kubectl delete deployment {self.faulty_service} -n {self.namespace} --wait=true")
        self.kubectl.exec_command_checked(f"kubectl apply -f {path} -n {self.namespace}")
        self.injected_threshold = self.injector.inject_disk_pressure(node_name=self.target_node)
        print(f"Service: {self.faulty_service} | Node: {self.target_node} | Namespace: {self.namespace}\n")

    def _state_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-eviction-node")

    @mark_fault_injected
    def recover_fault(self):
        if self.target_node.startswith("<") and self._state_path().exists():
            self.target_node = self._state_path().read_text().strip()
        super().recover_fault()
        # Evicted pods stay behind as Failed pods.
        self.kubectl.exec_command(f"kubectl delete pods -n {self.namespace} --field-selector=status.phase=Failed")
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )


# Stdlib-only HTTP client run by the surge Deployment: every request opens a new
# connection, so kube-proxy picks an endpoint per request.
_SURGE_CLIENT = r"""
import os, threading, urllib.request
target = os.environ["SURGE_TARGET"]
def loop(i):
    n = 0
    while True:
        n += 1
        url = "%s/channels/chan-%d/messages?limit=50" % (target, (i + n) % 16)
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except Exception:
            pass
for i in range(int(os.environ.get("SURGE_THREADS", "32"))):
    threading.Thread(target=loop, args=(i,), daemon=True).start()
threading.Event().wait()
"""


class WorkloadImbalanceSlack(WorkloadImbalance):
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
        self.faulty_service = [faulty_service]
        ported(
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


# Original registry id -> (ported problem id, problem class). Variants of one
# fault on different original apps map to the same port.
PORTS: dict[str, tuple[str, type]] = {
    "resource_request_too_large": ("resource_request_too_large_slack_spine", ResourceRequestTooLargeSlack),
    "pod_anti_affinity_deadlock": ("pod_anti_affinity_deadlock_slack_spine", PodAntiAffinityDeadlockSlack),
    "taint_no_toleration_social_network": ("taint_no_toleration_slack_spine", TaintNoTolerationSlack),
    "persistent_volume_affinity_violation": (
        "persistent_volume_affinity_violation_slack_spine",
        PersistentVolumeAffinityViolationSlack,
    ),
    "hpa_missing_effective_cpu_request_hotel_reservation": (
        "hpa_missing_effective_cpu_request_slack_spine",
        HPAMissingEffectiveCPURequestSlack,
    ),
    "cfs_cpu_throttling_hotel_reservation": ("cfs_cpu_throttling_slack_spine", CpuThrottlingSlack),
    "kafka_queue_problems": ("kafka_queue_problems_slack_spine", KafkaQueueProblemsSlack),
    "kubelet_eviction_threshold_misconfig": (
        "kubelet_eviction_threshold_misconfig_slack_spine",
        KubeletEvictionThresholdMisconfigSlack,
    ),
    "workload_imbalance": ("workload_imbalance_slack_spine", WorkloadImbalanceSlack),
}
