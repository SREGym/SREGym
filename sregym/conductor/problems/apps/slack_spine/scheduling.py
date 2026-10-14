"""Slack Spine: scheduling."""

from __future__ import annotations

import copy
import json
import time

from sregym.conductor.oracles.duplicate_pvc_mounts_mitigation import DuplicatePVCMountsMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.apps.slack_spine.manifests import _clean_deployment_manifest, _selector
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.duplicate_pvc_mounts import DuplicatePVCMounts
from sregym.conductor.problems.environment import (
    configure_port,
    make_app,
)
from sregym.conductor.problems.persistent_volume_affinity_violation import PersistentVolumeAffinityViolation
from sregym.conductor.problems.resource_request import ResourceRequest
from sregym.conductor.problems.taint_no_toleration import TaintNoToleration
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected


class ResourceRequestTooLargeSlack(ResourceRequest):
    """svc-channel requests twice the largest node's memory; its recreated pod never schedules.

    svc-channel resolves channel authorization for every message send, so
    posts fail while it is down (svc-search, the plan's first choice, carries
    under 1% of the load generator's sessions and the outage stayed invisible).
    """

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-channel"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        configure_port(
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


class PodAntiAffinityDeadlockSlack(Problem):
    """svc-thread gets a required pod anti-affinity that no node can satisfy.

    The anti-affinity term selects the chart-wide label every Slack Spine pod
    carries (``app.kubernetes.io/name=slack-spine``) per hostname, so each
    worker already hosts a matching pod and every svc-thread replica is Pending
    ("didn't match pod anti-affinity rules"). As in the original, the
    Deployment is re-created with more replicas than nodes.
    """

    ANTI_AFFINITY_LABELS = {"app.kubernetes.io/name": "slack-spine"}

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-thread"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.original_replicas: int | None = None
        configure_port(
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
        dyaml = _clean_deployment_manifest(self.injector._get_deployment_yaml(self.faulty_service))
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
        dyaml = _clean_deployment_manifest(self.injector._get_deployment_yaml(self.faulty_service))
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
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        configure_port(
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
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        configure_port(
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


class DuplicatePVCMountsIA(DuplicatePVCMounts):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-search"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        configure_port(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The deployment `{faulty_service}` is scaled to multiple replicas that all mount one "
                f"ReadWriteOnce PVC (`{faulty_service}-pvc`) while required hostname podAntiAffinity places those "
                "replicas on different nodes. With node-bound storage only one replica can use the claim, so "
                "another replica stays Pending and the deployment remains only partially Ready."
            ),
            oracle_factory=lambda problem: DuplicatePVCMountsMitigationOracle(
                problem, query_port=8000, query_path="/healthz", expected_content=""
            ),
        )
