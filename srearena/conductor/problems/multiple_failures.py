"""Simulating multiple failures in microservice applications, implemented by composing multiple single-fault problems."""

import time

from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.social_network import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.service.composite_app import CompositeApp
from srearena.utils.decorators import mark_fault_injected


class MultipleIndependentFailures(Problem):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        apps = [p.app for p in problems]
        self.app = CompositeApp(apps)
        self.namespaces = [p.namespace for p in problems]

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        for p in self.problems:
            p.inject_fault()
            time.sleep(2)
        self.faults_str = " | ".join([f"{p.__name__}" for p in self.problems])
        print(
            f"Injecting Fault: Multiple faults from included problems: [{self.faults_str}]| Namespace: {self.namespaces}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for p in self.problems:
            p.recover_fault()
            time.sleep(2)
        print(
            f"Recovering Fault: Multiple faults from included problems: [{self.faults_str}]| Namespace: {self.namespaces}\n"
        )
