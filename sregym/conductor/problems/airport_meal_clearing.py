"""Airport meal clearing after the overnight handover."""

from threading import Lock

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.airport_meals.application import AirportMealClearingApplication
from sregym.utils.decorators import mark_fault_injected


class AirportMealClearing(Problem):
    run_default_workload = False
    recover_before_undeploy = False

    def __init__(self):
        from sregym.conductor.oracles.airport_meal_clearing_mitigation import AirportMealClearingOracle
        from sregym.conductor.problems.airport_meal_stream import Stream

        super().__init__(app=AirportMealClearingApplication())
        self.kubectl = self.app.kubectl
        self.stream = Stream(namespace=self.namespace)
        self.airport_meal_expected = None
        self.airport_meal_history_phases = []
        self._setup_lock = Lock()
        self.root_cause = self.build_structured_root_cause(
            component="Carrier early departure close export selection",
            namespace=self.namespace,
            description=(
                "The north carrier close compiled its immutable lines from the configured primary-early interval "
                "on issue-orders. For the same independently approved early-bank cohorts, the sealed approval and "
                "receipt pairs show that clearing admitted the later contingency-early publisher's interval on "
                "preview-orders. A separate publisher wrote rehearsal-overnight on the same topic ID. "
                "On every output partition, the rehearsal interval's exclusive end is the contingency "
                "interval's inclusive start. The rehearsal interval supplied none of "
                "the admitted cohort rows. Equivalent descriptions of the primary versus admitted release "
                "relationship are valid without literal range names or offsets. "
                "Join the carrier close and statement reference, carrier cohort approvals, "
                "clearing export receipts, and the writer intervals by topic ID and per-partition bounds. "
                "A singly owned partition published revision 1 inside the frozen primary export and revision 2 "
                "after its upper bound. The admitted contingency included revision 2. The primary reader also "
                "missed two passengers on the same zero-owner partition, and the contingency approval covered "
                "only one of them. An equal-amount cohort has different beneficiaries in the two ranges. "
                "The genuine Flink 1.18.1 compressed 5-to-3 restore damaged ownership and explains earlier "
                "missing and duplicated output. An open input transaction delayed committed work until abort. "
                "Neither explains why the carrier used a different reference from clearing for the approved cohorts."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = AirportMealClearingOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        from sregym.conductor.problems.airport_meal_history import build

        with self._setup_lock:
            build(self)

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        from sregym.conductor.problems.airport_meal_history import recover

        recover(self)
