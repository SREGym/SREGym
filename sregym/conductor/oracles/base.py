"""Base class for evaluation oracles."""

from abc import ABC, abstractmethod

from sregym.conductor.oracles.failure import classify


class Oracle(ABC):
    #: Reason codes this oracle uses that the shared table does not cover, or
    #: covers differently. Consulted before ``SHARED_FAILURE_CLASSES``, so a
    #: subclass can take its own view of a shared reason without editing the
    #: shared table. Subclasses that only use shared reasons leave this empty.
    FAILURE_CLASSES: dict[str, str] = {}

    def __init__(self, problem):
        self.problem = problem

    def capture_baseline(self) -> None:
        """Record the healthy pre-fault cluster state.

        Called once the app is deployed and before the fault is injected.
        Oracles cannot do this in __init__: the Problem is constructed before
        deploy_app(), when the namespace does not exist yet. Defaults to a
        no-op for oracles that need no baseline.
        """
        return

    @classmethod
    def fail(cls, reason: str, **detail) -> dict:
        """Build a failure verdict that says why, and whose fault it was.

        ``reason`` is a stable snake_case code and is the part consumers may
        filter and aggregate on -- it reaches the results CSV as
        ``Mitigation.reason``. ``detail`` is free-form and diagnostic rather
        than a contract: consumers must tolerate it being absent or differently
        shaped between oracles.

        This is additive to the ``print`` an oracle already emits. The two have
        different consumers and neither replaces the other: the print goes to
        the run log and is what someone reads when a single run looks wrong, and
        it can carry detail too unwieldy for a column. No existing print should
        be removed in favour of a reason code.
        """
        verdict = {
            "success": False,
            "reason": reason,
            "failure_class": classify(reason, cls.FAILURE_CLASSES),
        }
        if detail:
            verdict["detail"] = detail
        return verdict

    @abstractmethod
    def evaluate(self, solution, trace, duration) -> dict:
        """Evaluate a solution."""
        pass
