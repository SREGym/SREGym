import logging
from srearena.conductor.oracles.compound import CompoundedOracle
from srearena.conductor.oracles.localization import LocalizationOracle
from srearena.conductor.oracles.mitigation import MitigationOracle
from srearena.conductor.oracles.workload import WorkloadOracle
from srearena.conductor.problems.base import Problem
from srearena.generators.fault.inject_tt import TrainTicketFaultInjector  
from srearena.service.apps.train_ticket import TrainTicket
from srearena.service.kubectl import KubeCtl
from srearena.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class TrainTicketF17(Problem):
    def __init__(self):
        self.app_name = "train-ticket"
        self.faulty_service = "ts-voucher-service"
        self.fault_name = "fault-17-nested-sql-select-clause-error"
        self.app = TrainTicket()

        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.localization_oracle = LocalizationOracle(problem=self, expected=[self.faulty_service])
        
        self.app.create_workload()
        self.mitigation_oracle = CompoundedOracle(
            self,
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector = TrainTicketFaultInjector(namespace=self.namespace)
        self.injector._inject(
            fault_type="fault-17-nested-sql-select-clause-error",
        )
        print(f"Injected fault-17-nested-sql-select-clause-error | Namespace: {self.namespace}\n")


    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector = TrainTicketFaultInjector(namespace=self.namespace)
        self.injector._recover(
            fault_type="fault-17-nested-sql-select-clause-error",
        )
        print(f"Recovered from fault-17-nested-sql-select-clause-error | Namespace: {self.namespace}\n")
