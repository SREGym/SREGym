import logging

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_tt import TrainTicketFaultInjector
from sregym.service.apps.train_ticket import TrainTicket
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class TrainTicketF22(Problem):
    def __init__(self):
        self.app_name = "train-ticket"
        self.faulty_service = "ts-contacts-service"
        self.fault_name = "fault-22-sql-column-name-mismatch-error"
        self.app = TrainTicket()

        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "Injected SQL statements reference an incorrect column name, so database queries in the contacts service "
                "fail at execution time and related API operations return errors."
            ),
        )
        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector = TrainTicketFaultInjector(namespace=self.namespace)
        self.injector._inject(
            fault_type="fault-22-sql-column-name-mismatch-error",
        )
        print(f"Injected fault-22-sql-column-name-mismatch-error | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector = TrainTicketFaultInjector(namespace=self.namespace)
        self.injector._recover(
            fault_type="fault-22-sql-column-name-mismatch-error",
        )
        print(f"Recovered from fault-22-sql-column-name-mismatch-error | Namespace: {self.namespace}\n")
