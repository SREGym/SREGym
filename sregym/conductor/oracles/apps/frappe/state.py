"""Frappe: state."""

from __future__ import annotations

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle


class FrappeFaultStateOracle(MitigationOracle):
    """The generic workload health check, then the port's own fault-state check.

    ``problem.fault_check(oracle)`` returns ``None`` when the injected fault is
    gone, or a failure verdict (built with ``oracle.fail``) describing why not.
    """

    FAILURE_CLASSES = {
        "site_database_unusable": FailureClass.AGENT_ERROR,
    }

    def evaluate(self) -> dict:
        result = super().evaluate()
        try:
            verdict = self.problem.fault_check(self)
        except Exception as exc:
            print(f"❌ Fault-state check raised: {exc}")
            verdict = self.fail_from_exception(exc)
        # The fault-specific verdict is the more telling reason when both fail.
        return verdict or result
