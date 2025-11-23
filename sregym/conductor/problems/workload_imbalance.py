import time

from sregym.conductor.oracles.imbalance_mitigation import ImbalanceMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class WorkloadImbalance(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = ["frontend"]
        self.injector = VirtualizationFaultInjector(namespace="kube-system")
        self.injector_for_scale = VirtualizationFaultInjector(namespace=self.namespace)
        self.root_cause = "The kube-proxy daemonset is using a buggy image version, and the frontend deployment is scaled to 5 replicas with a high workload surge, causing workload imbalance across pods."
        super().__init__(app=self.app, namespace=self.namespace)

        # not so precise here by now
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.mitigation_oracle = ImbalanceMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_daemon_set_image_replacement(
            daemon_set_name="kube-proxy", new_image="docker.io/jackcuii/kube-proxy:v1.31.12"
        )
        print(f"Service: {self.faulty_service[0]} | Namespace: {self.namespace}\n")
        self.injector_for_scale.scale_pods_to(replicas=5, microservices=self.faulty_service)
        self.kubectl.wait_for_ready(namespace=self.namespace)
        # surge the workload
        print("== Surge the workload ==")
        self.app.wrk.change_users(number=500, namespace=self.namespace)
        self.app.wrk.change_spawn_rate(rate=50, namespace=self.namespace)
        print("== Wait the workload to be stable ==")
        time.sleep(10)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.inject_daemon_set_image_replacement(
            daemon_set_name="kube-proxy", new_image="registry.k8s.io/kube-proxy:v1.31.13"
        )
        print(f"Service: {self.faulty_service[0]} | Namespace: {self.namespace}\n")
        self.injector_for_scale.scale_pods_to(replicas=1, microservices=self.faulty_service)
        self.kubectl.wait_for_ready(namespace=self.namespace)
        # reduce the workload
        print("== Reduce the workload ==")
        self.app.wrk.change_users(number=10, namespace=self.namespace)
        self.app.wrk.change_spawn_rate(rate=1, namespace=self.namespace)
