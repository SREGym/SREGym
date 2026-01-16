"""Simulating multiple failures in microservice applications, implemented by composing multiple single-fault problems."""

import time

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.composite_app import CompositeApp
from sregym.utils.decorators import mark_fault_injected


class MultipleIndependentFailures(Problem):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        apps = [p.app for p in problems]
        self.app = CompositeApp(apps)
        self.namespaces = [p.namespace for p in problems]
        self.fault_injected = False

        # === Attaching problem's oracles ===
        # diagnosis oracles can be statically defined.
        # concat all root causes together.
        self.root_cause: str = "This problem contains multiple faults.\n"
        for p in self.problems:
            root_cause = "" if p.root_cause is None else p.root_cause
            self.root_cause += root_cause
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # mitigation oracle can and should be dynamic
        mitigation_oracles = [p.mitigation_oracle for p in self.problems]
        if len(mitigation_oracles) > 0:
            self.mitigation_oracle = CompoundedOracle(self, *mitigation_oracles)

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
