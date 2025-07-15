from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_virtual import VirtualizationFaultInjector
from srearena.service.apps.astronomy_shop import AstronomyShop
from srearena.service.apps.hotelres import HotelReservation
from srearena.service.apps.socialnet import SocialNetwork
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected
from srearena.utils.randomizer import Randomizer
from srearena.conductor.oracles.rolling_update_misconfiguration_mitigation import RollingUpdateMitigationOracle

class RollingUpdateMisconfigured(Problem):
    def __init__(self):    
        self.kubectl = KubeCtl()
        self.randomizer = Randomizer(kubectl=self.kubectl)
        self.app = self.randomizer.select_app(["Social Network", "Hotel Reservation"])
        
        super().__init__(app=self.app, namespace=self.app.namespace)
        
        self.app.create_workload()

    def decide_targeted_service(self):
        self.faulty_service = "custom-service"

        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
        self.mitigation_oracle = CompoundedOracle(
            self,
            RollingUpdateMitigationOracle(problem=self,deployment_name=self.faulty_service),
            WorkloadOracle(problem=self,wrk_manager=self.app.wrk)
        )
                
    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(fault_type="rolling_update_misconfigured",
            microservices=[self.faulty_service])
        
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
        
    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type="rolling_update_misconfigured",
            microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
        
        
        