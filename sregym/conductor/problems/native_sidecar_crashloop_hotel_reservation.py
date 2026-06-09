"""
SREGym problem: native_sidecar_crashloop_hotel_reservation

A native sidecar container (Kubernetes 1.32 GA feature) is injected
into the frontend deployment with a startupProbe that always fails.
The sidecar enters CrashLoopBackOff. The main application container
is blocked in Init phase permanently.
"""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import (
    LLMAsAJudgeOracle,
)
from sregym.conductor.oracles.native_sidecar_mitigation import (
    NativeSidecarMitigationOracle,
)
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

TARGET_DEPLOYMENT = "frontend"
SNAPSHOT_DIR = "/tmp"


class NativeSidecarCrashloopHotelReservation(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{TARGET_DEPLOYMENT}",
            namespace=self.namespace,
            description=(
                f"A native sidecar container named 'otel-collector-sidecar' "
                f"(initContainer with restartPolicy: Always) "
                f"was added to the '{TARGET_DEPLOYMENT}' "
                f"deployment. The sidecar's startupProbe checks for "
                f"/tmp/otel-ready, a file that is never created. The probe "
                f"always fails. After exceeding failureThreshold, the kubelet "
                f"restarts the sidecar, which enters CrashLoopBackOff. "
                f"Because Kubernetes requires native sidecars to reach 'started' "
                f"state before app containers begin, the main container "
                f"is permanently blocked in the Init phase. Old pods "
                f"continue Running. The fix is to remove the "
                f"'otel-collector-sidecar' initContainer from the deployment "
                f"spec, or to fix its startupProbe."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(
            problem=self,
            expected=self.root_cause,
        )

        self.mitigation_oracle = NativeSidecarMitigationOracle(
            problem=self,
            deployment_name=TARGET_DEPLOYMENT,
        )

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_native_sidecar_crashloop(
            deployment_name=TARGET_DEPLOYMENT,
            namespace=self.namespace,
            snapshot_dir=SNAPSHOT_DIR,
        )
        print(
            f"Service: {TARGET_DEPLOYMENT} | Namespace: {self.namespace}\n"
            f"Native sidecar 'otel-collector-sidecar' injected with "
            f"always-failing startupProbe.\n"
            f"New pods will be blocked in Init:0/1 indefinitely.\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_native_sidecar_crashloop(
            deployment_name=TARGET_DEPLOYMENT,
            namespace=self.namespace,
            snapshot_dir=SNAPSHOT_DIR,
        )
        print(
            f"Service: {TARGET_DEPLOYMENT} | Namespace: {self.namespace}\n"
            f"Native sidecar removed. Deployment restored.\n"
        )
