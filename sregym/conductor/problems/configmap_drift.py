"""ConfigMap drift problem - removes critical keys from mounted ConfigMap."""

from sregym.conductor.oracles.configmap_itself_localization_oracle import ConfigMapItselfLocalizationOracle
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

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.localization_oracle = ConfigMapItselfLocalizationOracle(
            problem=self, namespace=self.namespace, expected_configmap_name=f"{self.faulty_service}-config"
        )
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
