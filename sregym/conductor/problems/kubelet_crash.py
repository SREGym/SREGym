from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class KubeletCrash(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.rollout_services = ["frontend", "frontend-proxy", "currency"]
        self.injector = RemoteOSFaultInjector()

        self.root_cause = "The kubelet daemon on a node has crashed, preventing pod scheduling, updates, and management on that node, causing services to become unavailable or stuck."

        # === Attach evaluation oracles ===
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_kubelet_crash()
        # rollout restart selected services for faster symptom
        for service in self.rollout_services:
            print(f"Rolling out {service}...")
            self.kubectl.trigger_rollout(deployment_name=service, namespace=self.namespace)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_kubelet_crash()
        # rollout restart all services for faster recovery
        self.kubectl.exec_command(f"kubectl rollout restart deployment -n {self.namespace}")
        self.kubectl.wait_for_ready(self.namespace)
