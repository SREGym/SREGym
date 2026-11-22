"""Saleor: state."""

from __future__ import annotations

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle


class SaleorFaultStateOracle(MitigationOracle):
    """The generic workload health check, then the port's own fault-state check.

    ``problem.fault_check(oracle)`` returns ``None`` when the injected fault is
    gone (and the user path it broke works again), or a failure verdict built
    with ``oracle.fail``.
    """

    FAILURE_CLASSES = {
        "checkout_probe_failed": FailureClass.AGENT_ERROR,
    }

    def evaluate(self) -> dict:
        result = super().evaluate()
        if not result.get("success"):
            return result
        try:
            verdict = self.problem.fault_check(self)
        except Exception as exc:
            print(f"❌ Fault-state check raised: {exc}")
            return self.fail_from_exception(exc)
        return verdict or {"success": True}
