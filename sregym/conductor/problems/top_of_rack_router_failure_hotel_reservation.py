import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from sregym.paths import FAULT_SCRIPTS, TARGET_MICROSERVICES
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.apps.helpers import get_frontend_url
from sregym.service.kubectl import KubeCtl
from sregym.generators.workload.wrk2 import Wrk2, Wrk2WorkloadManager

from sregym.conductor.oracles.mitigation import MitigationOracle

logger = logging.getLogger("all.problem")
logger.propagate = True
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class ToRConfig:
    chaos_name: str = "tor-router-partition"
    node_label_key: str = "sregym.io/tor-node"
    pod_group_label_key: str = "sregym.io/tor-group"
    victim_group: str = "a"
    rest_group: str = "b"

    victim_deploy_name_substrings: tuple = ("frontend",)

    rollout_timeout_s: int = 300
    settle_sleep_s: int = 5


class ChaosMeshToRFault:
    """
    Implements fault injection:
    - spawn >=2 worker nodes
    - deterministically place pods via nodeSelector and pod labels
    - apply NetworkChaos partition with ChaosMesh to simulate a network partition
    - provide idempotent recovery/cleanup
    """

    def __init__(self, kubectl: KubeCtl, namespace: str, chaos_yaml_config: Path, cfg: ToRConfig):
        self.kubectl = kubectl
        self.ns = namespace
        self.chaos_yaml_config = chaos_yaml_config
        self.cfg = cfg

    def inject(self) -> None:
        self._cleanup()

        victim_node, rest_nodes = self._select_nodes()
        self._label_nodes(victim_node, rest_nodes)

        deployments = self._list_deployments()
        self._label_and_run_deployments(deployments)

        self._wait_rollouts(deployments)
        time.sleep(self.cfg.settle_sleep_s)

        self._apply_networkchaos()

    def recover(self) -> None:
        self._delete_networkchaos()

        deployments = self._list_deployments()
        self._teardown_deployments(deployments)
        self._wait_rollouts(deployments)

        self._unlabel_nodes()

    def _cleanup(self) -> None:
        self._delete_networkchaos()
        deployments = self._list_deployments()
        self._teardown_deployments(deployments)
        self._unlabel_nodes()

    def _select_nodes(self) -> tuple[str, List[str]]:
        """Pick nodes to fail (NOTE: This needs >=2 worker node)"""
        raw = self.kubectl.exec_command("kubectl get nodes -o json")
        data = json.loads(raw)

        worker_nodes: List[str] = []
        for item in data["items"]:
            name = item["metadata"]["name"]
            labels = item["metadata"].get("labels", {})

            if "node-role.kubernetes.io/control-plane" in labels:
                continue
            if "node-role.kubernetes.io/master" in labels:
                continue

            worker_nodes.append(name)

        if len(worker_nodes) < 2:
            raise RuntimeError(
                f"Top-of-Rack problem requires >=2 worker nodes, found {len(worker_nodes)}."
            )

        victim = worker_nodes[0]
        rest = worker_nodes[1:]
        return victim, rest

    def _label_nodes(self, victim_node: str, rest_nodes: List[str]) -> None:
        # NOTE: Label failure node as group "A" (TODO: This appears adhoc, fix)
        self.kubectl.exec_command(
            f"kubectl label node {victim_node} {self.cfg.node_label_key}={self.cfg.victim_group} --overwrite"
        )
        for n in rest_nodes:
            self.kubectl.exec_command(
                f"kubectl label node {n} {self.cfg.node_label_key}={self.cfg.rest_group} --overwrite"
            )

    def _unlabel_nodes(self) -> None:
        raw = self.kubectl.exec_command("kubectl get nodes -o json")
        data = json.loads(raw)
        for item in data["items"]:
            name = item["metadata"]["name"]
            labels = item["metadata"].get("labels", {})
            if self.cfg.node_label_key in labels:
                self.kubectl.exec_command(f"kubectl label node {name} {self.cfg.node_label_key}-")

    def _list_deployments(self) -> List[str]:
        raw = self.kubectl.exec_command(f"kubectl get deploy -n {self.ns} -o json")
        data = json.loads(raw)
        return [d["metadata"]["name"] for d in data["items"]]

    def _is_failure_target_node_deployed(self, deploy_name: str) -> bool:
        dn = deploy_name.lower()
        return any(substr in dn for substr in self.cfg.victim_deploy_name_substrings)

    def _label_and_run_deployments(self, deployments: List[str]) -> None:
        for d in deployments:
            group = self.cfg.victim_group if self._is_failure_target_node_deployed(d) else self.cfg.rest_group

            patch = {
                "spec": {
                    "template": {
                        "metadata": {"labels": {self.cfg.pod_group_label_key: group}},
                        "spec": {"nodeSelector": {self.cfg.node_label_key: group}},
                    }
                }
            }
            patch_str = json.dumps(patch)
            self.kubectl.exec_command(
                f"kubectl patch deploy {d} -n {self.ns} --type=merge -p '{patch_str}'"
            )

    def _teardown_deployments(self, deployments: List[str]) -> None:
        for d in deployments:
            patch = {
                "spec": {
                    "template": {
                        "metadata": {"labels": {self.cfg.pod_group_label_key: None}},
                        "spec": {"nodeSelector": {self.cfg.node_label_key: None}},
                    }
                }
            }
            patch_str = json.dumps(patch)
            self.kubectl.exec_command(
                f"kubectl patch deploy {d} -n {self.ns} --type=merge -p '{patch_str}'"
            )

    def _wait_rollouts(self, deployments: List[str]) -> None:
        for d in deployments:
            self.kubectl.exec_command(
                f"kubectl rollout status deploy/{d} -n {self.ns} --timeout={self.cfg.rollout_timeout_s}s"
            )

    def _apply_networkchaos(self) -> None:
        self.kubectl.exec_command(f"kubectl apply -n {self.ns} -f {self.chaos_yaml_config}")

    def _delete_networkchaos(self) -> None:
        self.kubectl.exec_command(
            f"kubectl delete networkchaos {self.cfg.chaos_name} -n {self.ns} --ignore-not-found=true"
        )


class TopOfRackRouterFailureHotelReservation:
    problem_id = "top_of_rack_router_failure_hotel_reservation"

    def __init__(self):
        self.kubectl = KubeCtl()
        self.app = BlueprintHotelReservation()

        self.cfg = ToRConfig()
        self.chaos_yaml = FAULT_SCRIPTS / "tor-router-partition.yaml"

        self.fault = ChaosMeshToRFault(
            kubectl=self.kubectl,
            namespace=self.app.namespace,
            chaos_yaml_config=self.chaos_yaml,
            cfg=self.cfg,
        )
        self.wrk = Wrk2WorkloadManager(
            wrk=Wrk2(
                rate=100,
                dist="exp",
                connections=100,
                duration=30,
                threads=3,
                namespace=self.app.namespace,
            ),
            payload=None, # TODO: This was hardcoded, now it's removed
            url="{placeholder}",
            namespace=self.app.namespace,
        )

        self.oracle = MitigationOracle(app=self.app)

    def setup(self) -> None:
        # Start from clean state (see Issue #280)
        self.fault.preclean()

        self.app.deploy()

        # Start workload (TODO: check through cli.py)
        self.wrk.url = get_frontend_url(self.app)
        self.wrk.start()

    def inject_fault(self) -> None:
        self.fault.inject()

    def recover(self) -> None:
        self.fault.recover()

    def cleanup(self) -> None:
        try:
            self.wrk.stop()
        except Exception:
            pass
        try:
            self.fault.preclean()
        except Exception:
            pass
        self.app.cleanup()
