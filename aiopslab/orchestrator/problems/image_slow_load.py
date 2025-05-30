"""Otel demo imageSlowLoad feature flag fault."""

from aiopslab.generators.fault.inject_otel import OtelFaultInjector
from aiopslab.orchestrator.oracles.detection import DetectionOracle
from aiopslab.orchestrator.oracles.localization import LocalizationOracle
from aiopslab.orchestrator.problems.base import Problem
from aiopslab.service.apps.astronomy_shop import AstronomyShop
from aiopslab.service.kubectl import KubeCtl


class ImageSlowLoad(Problem):
    def __init__(self):
        self.app = AstronomyShop()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.injector = OtelFaultInjector(namespace=self.namespace)
        self.faulty_service = "frontend"

        # === Attach evaluation oracles ===
        self.detection_oracle = DetectionOracle(problem=self, expected="Yes")

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])

    def start_workload(self):
        print("== Start Workload ==")
        print("Workload skipped since AstronomyShop has a built-in load generator.")

    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector.inject_fault("imageSlowLoad")
        print(f"Fault: imageSlowLoad | Namespace: {self.namespace}\n")

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector.recover_fault("imageSlowLoad")
