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
        # Build an explicit multi-fault narrative with clear per-fault boundaries.
        fault_sections: list[str] = []
        for idx, p in enumerate(self.problems, start=1):
            root_cause = (p.root_cause or "").strip()
            if not root_cause:
                continue
            fault_sections.append(f"Fault {idx} ({p.__class__.__name__}):\n{root_cause}")

        if fault_sections:
            self.root_cause = (
                "This scenario contains multiple independent faults across different components. "
                "Each fault and its symptoms are listed below.\n\n" + "\n\n".join(fault_sections)
            )
        else:
            self.root_cause = (
                "This scenario contains multiple independent faults, but no sub-fault root causes were provided."
            )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # mitigation oracle: compound of all sub-problem mitigation oracles
        mitigation_oracles = [p.mitigation_oracle for p in self.problems if p.mitigation_oracle]
        if mitigation_oracles:
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
