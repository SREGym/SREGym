import logging

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.workload import WorkloadOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_tt import TrainTicketFaultInjector
from sregym.service.apps.train_ticket import TrainTicket
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class TrainTicketF17(Problem):
    def __init__(self):
        self.app_name = "train-ticket"
        self.faulty_service = "ts-voucher-service"
        self.fault_name = "fault-17-nested-sql-select-clause-error"
        self.app = TrainTicket()

        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.root_cause = f"The deployment `{self.faulty_service}` has a nested SQL SELECT clause error in its database queries, causing database operation failures."

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.resolution_oracle = CompoundedOracle(
            self,
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
        )
        self.mitigation_oracle = AlertOracle(problem=self)

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
