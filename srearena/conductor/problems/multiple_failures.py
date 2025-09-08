"""Simulating multiple failures in microservice applications, implemented by composing multiple single-fault problems."""

import time

from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.composite_app import CompositeApp
from srearena.service.apps.social_network import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class MultipleIndependentFailures(Problem):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        apps = [p.app for p in problems]
        self.app = CompositeApp(apps)
        self.namespaces = [p.namespace for p in problems]
        self.fault_injected = False

        # === Attaching problem's oracles ===
        localization_oracles = [p.localization_oracle for p in self.problems]
        if len(localization_oracles) > 0:
            print(f"[MIF] Localization oracles: {localization_oracles}")
            self.localization_oracle = CompoundedOracle(
                self,
                *localization_oracles
            )

        mitigation_oracles = [p.mitigation_oracle for p in self.problems]
        if len(mitigation_oracles) > 0:
            print(f"[MIF] Mitigation oracles: {mitigation_oracles}")
            self.mitigation_oracle = CompoundedOracle(
                self,
                *mitigation_oracles
            )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        for p in self.problems:
            print(f"Injecting Fault: {p.__class__.__name__} | Namespace: {p.namespace}")
            p.inject_fault()
            time.sleep(1)
        self.faults_str = " | ".join([f"{p.__class__.__name__}" for p in self.problems])
        print(
            f"Injecting Fault: Multiple faults from included problems: [{self.faults_str}] | Namespace: {self.namespaces}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        for p in self.problems:
            print(f"Recovering Fault: {p.__class__.__name__} | Namespace: {p.namespace}")
            p.recover_fault()
            time.sleep(1)
        print(
            f"Recovering Fault: Multiple faults from included problems: [{self.faults_str}] | Namespace: {self.namespaces}\n"
        )
