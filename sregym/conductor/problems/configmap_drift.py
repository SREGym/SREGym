"""ConfigMap drift problem - removes critical keys from mounted ConfigMap."""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.missing_cm_key_mitigation import MissingCmKeyMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ConfigMapDrift(Problem):
    def __init__(self, faulty_service: str = "geo"):
        self.faulty_service = faulty_service

        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"ConfigMap `{self.faulty_service}-config` has drifted and is missing required keys (for example "
                "`GeoMongoAddress`), so deployment `{self.faulty_service}` starts with incomplete runtime config. "
                "The service may run but fails dependency initialization and request handling for paths requiring the "
                "missing settings. Users observe intermittent errors, degraded responses, and feature-level failures."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.configmap_name = f"{self.faulty_service}-config"

        self.app.create_workload()
        self.mitigation_oracle = MissingCmKeyMitigationOracle(
            problem=self,
            configmap_name=self.configmap_name,
            expected_keys=[
                "consulAddress",
                "jaegerAddress",
                "FrontendPort",
                "GeoPort",
                "GeoMongoAddress",
                "ProfilePort",
                "ProfileMongoAddress",
                "ProfileMemcAddress",
                "RatePort",
                "RateMongoAddress",
                "RateMemcAddress",
                "RecommendPort",
                "RecommendMongoAddress",
                "ReservePort",
                "ReserveMongoAddress",
                "ReserveMemcAddress",
                "SearchPort",
                "UserPort",
                "UserMongoAddress",
                "KnativeDomainName",
            ],
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_configmap_drift(microservices=[self.faulty_service])

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_configmap_drift(microservices=[self.faulty_service])

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
