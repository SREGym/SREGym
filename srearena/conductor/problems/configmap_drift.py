"""ConfigMap drift problem - removes critical keys from mounted ConfigMap."""

from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.missing_cm_key_mitigation import MissingCmKeyMitigationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.apps.socialnet import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected


class ConfigMapDrift(Problem):
    def __init__(self, faulty_service: str = "geo"):
        self.faulty_service = faulty_service

        self.app = HotelReservation()

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
        self.configmap_name = f"{self.faulty_service}-config"

        self.app.create_workload()
        self.mitigation_oracle = CompoundedOracle(
            self,
            MissingCmKeyMitigationOracle(problem=self, configmap_name=self.configmap_name, expected_keys=[
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
                "KnativeDomainName"
            ]),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
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
