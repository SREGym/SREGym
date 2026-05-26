from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class DiskPressureEviction(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "currency"
        self.target_node = "kind-worker"
        self.injector = RemoteOSFaultInjector()

        self.root_cause = self.build_structured_root_cause(
            component=f"node/{self.target_node}",
            namespace=self.namespace,
            description=(
                f"Node `{self.target_node}` is reporting `DiskPressure=True` because the kubelet "
                f"`nodefs.available` eviction threshold in `/var/lib/kubelet/config.yaml` has been "
                f"raised above the node's actual filesystem free-space ratio. The "
                f"`{self.faulty_service}` Deployment is pinned to this node via `nodeName`, so "
                f"evicted pods are recreated on the same pressured node, producing a continuous "
                f"eviction loop and sustained service unavailability. Note: `df` will show ample "
                f"free disk; the cause is the misconfigured eviction threshold, not actual disk "
                f"exhaustion."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        # Pin target deployment to the worker we'll pressure
        self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} "
            f'--type=strategic -p=\'{{"spec":{{"template":{{"spec":{{"nodeName":"{self.target_node}"}}}}}}}}\''
        )
        # Trigger node-level disk pressure
        self.injector.inject_disk_pressure(node_name=self.target_node)
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

        print("Deleting evicted pods...")
        # TODO: this is taking too much time to recover. Figure out if there are any efficient way to do it.
        self.kubectl.exec_command(
            "kubectl delete pods --all-namespaces --field-selector=status.phase=Failed --ignore-not-found=true"
        )

        print("=== Fault Recovered ===")
