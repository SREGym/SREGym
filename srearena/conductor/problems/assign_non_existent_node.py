"""Assign pods to non existent node problem for the SocialNetwork application."""

import time

from srearena.conductor.oracles.assign_non_existent_node_mitigation import AssignNonExistentNodeMitigationOracle
from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.socialnet import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.utils.randomizer import Randomizer

class AssignNonExistentNode(Problem):
    def __init__(self):
        self.kubectl = KubeCtl()
        self.randomizer = Randomizer(kubectl=self.kubectl)
        app = self.randomizer.select_app()
        super().__init__(app=app, namespace=app.namespace)
        self.app.create_workload()

    def decide_targeted_service(self):
        self.faulty_service = self.randomizer.select_service()

        # === Attach evaluation oracles ===
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
        self.mitigation_oracle = CompoundedOracle(
            self,
            AssignNonExistentNodeMitigationOracle(problem=self),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="assign_to_non_existent_node",
            microservices=[self.faulty_service],
        )
        time.sleep(25)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="assign_to_non_existent_node",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
