from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
    KubeletEvictionThresholdMisconfigMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class KubeletEvictionThresholdMisconfig(Problem):
    # Static so fix_kubernetes() can reference it without instantiating the problem
    NAMESPACE = "astronomy-shop"

    def __init__(self, *, app: Application | None = None):
        if app is None:
            app = AstronomyShop()
            assert app.namespace == self.NAMESPACE, (
                f"AstronomyShop namespace {app.namespace!r} drifted from "
                f"KubeletEvictionThresholdMisconfig.NAMESPACE {self.NAMESPACE!r}"
            )
        super().__init__(app=app)
        self.kubectl = KubeCtl()
        self.faulty_service = "currency"
        self.injector = RemoteOSFaultInjector()
        self.target_node = self._pick_worker_node()
        self.injected_threshold: float | None = None

        self.root_cause = self.build_structured_root_cause(
            component=f"node/{self.target_node}",
            namespace=self.namespace,
            description=self._description(),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = KubeletEvictionThresholdMisconfigMitigationOracle(problem=self)

        self.app.create_workload()

    def _pick_worker_node(self) -> str:
        """Protect workload helpers and minimize collateral local-volume disruption."""
        pods = self.kubectl.core_v1_api.list_pod_for_all_namespaces().items
        protected_nodes = set()
        for pod in pods:
            labels = pod.metadata.labels or {}
            if pod.metadata.namespace == self.namespace and (
                labels.get("app") in {"locust-fetcher", "load-generator"}
                or labels.get("job") == "workload"
                or labels.get("app.kubernetes.io/component") == "loadgen"
                or labels.get("opentelemetry.io/name") == "load-generator"
            ):
                protected_nodes.add(pod.spec.node_name)
        candidates = [
            node.metadata.name
            for node in self.kubectl.list_nodes().items
            if "node-role.kubernetes.io/control-plane" not in (node.metadata.labels or {})
            and node.metadata.name not in protected_nodes
        ]
        if not candidates:
            raise RuntimeError("No worker is available without disrupting benchmark workload helpers.")

        def bound_volumes(node):
            return sum(
                1
                for pod in pods
                if pod.spec.node_name == node
                and any(volume.persistent_volume_claim for volume in (pod.spec.volumes or []))
            )

        return min(candidates, key=lambda node: (bound_volumes(node), node))

    def _description(self) -> str:
        return (
            f"Node `{self.target_node}` reports `DiskPressure=True` due to a misconfigured "
            f"`nodefs.available` eviction threshold in `/var/lib/kubelet/config.yaml`, not "
            f"actual disk exhaustion — `df` shows ample free space. Deployment "
            f"`{self.faulty_service}` is pinned via `nodeName` to the same node, producing a "
            f"continuous evict/recreate loop and sustained service unavailability."
        )

    def _prepare_injection(self):
        # Placement is known only after Conductor has deployed the workload.
        self.target_node = self._pick_worker_node()
        self.root_cause = self.build_structured_root_cause(
            component=f"node/{self.target_node}", namespace=self.namespace, description=self._description()
        )
        self.diagnosis_oracle.expected = self.root_cause

    @mark_fault_injected
    def inject_fault(self):
        self._prepare_injection()
        print("== Fault Injection ==")
        # Finish the pinned rollout before inducing pressure. Otherwise an old
        # healthy ReplicaSet can keep serving while the pinned replacement evicts.
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} "
            f'--type=strategic -p=\'{{"spec":{{"template":{{"spec":{{"nodeName":"{self.target_node}"}}}}}}}}\''
        )
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )
        # Trigger node-level disk pressure
        self.injected_threshold = self.injector.inject_disk_pressure(node_name=self.target_node)
        self.kubectl.exec_command_checked(
            f"kubectl wait node/{self.target_node} --for=condition=DiskPressure=True --timeout=180s"
        )
        print(f"Service: {self.faulty_service} | Node: {self.target_node} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # Restore kubelet eviction threshold so DiskPressure taint clears
        self.injector.recover_disk_pressure(node_name=self.target_node)

        print(f"Unpinning {self.faulty_service} deployment from pressured node...")
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} "
            f'--type=json -p=\'[{{"op":"remove","path":"/spec/template/spec/nodeName"}}]\''
        )
