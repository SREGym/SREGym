from sregym.conductor.oracles.kubelet_eviction_threshold_misconfig_mitigation import (
    KubeletEvictionThresholdMisconfigMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class KubeletEvictionThresholdMisconfig(Problem):
    # Static so fix_kubernetes() can reference it without instantiating the problem
    NAMESPACE = "astronomy-shop"

    def __init__(self):
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
            description=(
                f"Node `{self.target_node}` reports `DiskPressure=True` due to a misconfigured "
                f"`nodefs.available` eviction threshold in `/var/lib/kubelet/config.yaml`, not "
                f"actual disk exhaustion — `df` shows ample free space. Deployment "
                f"`{self.faulty_service}` is pinned via `nodeName` to the same node, producing a "
                f"continuous evict/recreate loop and sustained service unavailability."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = KubeletEvictionThresholdMisconfigMitigationOracle(problem=self)

        self.app.create_workload()

    def _pick_worker_node(self) -> str:
        """Return first worker node name from the cluster."""
        output = self.kubectl.exec_command("kubectl get nodes --no-headers")
        for line in output.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3 and "control-plane" not in parts[2]:
                return parts[0]
        raise RuntimeError("No worker nodes available for disk pressure injection.")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        # Pin target deployment to the worker we'll pressure
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} "
            f'--type=strategic -p=\'{{"spec":{{"template":{{"spec":{{"nodeName":"{self.target_node}"}}}}}}}}\''
        )
        # Trigger node-level disk pressure
        self.injected_threshold = self.injector.inject_disk_pressure(node_name=self.target_node)
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
