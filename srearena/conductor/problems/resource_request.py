import copy
from abc import abstractmethod

from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.utils.randomizer import Randomizer

class ResourceRequest(Problem):
    def __init__(self):
        self.kubectl = KubeCtl()
        self.randomizer = Randomizer(kubectl=self.kubectl)
        app = self.randomizer.select_app()
        super().__init__(app=app, namespace=app.namespace)
        self.app.create_workload()

    def decide_targeted_service(self):
        self.faulty_service = self.randomizer.select_service()

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
        self.mitigation_oracle = CompoundedOracle(
            self,
            MitigationOracle(problem=self),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="resource_request",
            microservices=[self.faulty_service],
            duration=self.set_memory_limit,  # Not a duration
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="resource_request",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @abstractmethod
    def set_memory_limit(self, deployment_yaml) -> dict:
        pass


class ResourceRequestTooLarge(ResourceRequest):
    def set_memory_limit(self, deployment_yaml):
        dyaml = copy.deepcopy(deployment_yaml)
        upper_limit = self.kubectl.get_node_memory_capacity()
        new_limit = self.kubectl.format_k8s_memory((upper_limit + 100 * 1024) * 2)
        dyaml["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] = new_limit
        print(f"Setting memory request to {new_limit} for {self.faulty_service}")
        return dyaml


class ResourceRequestTooSmall(ResourceRequest):
    def set_memory_limit(self, deployment_yaml):
        dyaml = copy.deepcopy(deployment_yaml)
        new_limit = "10Mi"
        dyaml["spec"]["template"]["spec"]["containers"][0]["resources"].setdefault("limits", dict())[
            "memory"
        ] = new_limit
        print(f"Setting memory limit to {new_limit} for {self.faulty_service}")
        return dyaml
