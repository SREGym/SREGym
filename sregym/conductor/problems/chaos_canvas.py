"""A healthy deployment for the chaos agent to break.

``ChaosCanvas`` injects nothing. The chaos agent works in the mitigation stage
with the same access as a fixer, can read the live SLO verdict from the
conductor's ``/chaos/slo`` endpoint (enabled only for this problem via
``expose_slo``), and submits once its artifact is written. The stage's
"oracle" records whether the SLO is violated at submission time; acceptance is
decided later by replaying the artifact on fresh deployments.
"""

from __future__ import annotations

from sregym.chaos import protected, telemetry
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.slo_oracle import SLOMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.generated_problem import FiringAlerts, make_app
from sregym.utils.decorators import mark_fault_injected


class ChaosImpactOracle(Oracle):
    """Succeeds when the fault the chaos agent left in place violates the SLO."""

    evaluation_timeout_seconds = 600

    def __init__(self, problem, slo_oracle: SLOMitigationOracle):
        super().__init__(problem)
        self.slo_oracle = slo_oracle

    def evaluate(self) -> dict:
        verdict = self.slo_oracle.measure(120)
        integrity = protected.compare(
            self.slo_oracle.protected_before or {}, protected.snapshot(self.problem.namespace)
        )
        detail = {"window": verdict.to_dict(), "new_alerts": self.problem.alerts.new(),
                  **self.problem.signals.changes(), "integrity": integrity}
        if not verdict.violated:
            return self.fail("slo_not_violated", **detail)
        return {"success": True, "detail": detail}


class ChaosCanvas(Problem):
    expose_slo = True

    def __init__(self, app_name: str):
        super().__init__(app=make_app(app_name))
        self.app.create_workload()
        # StreamWorkloadManager.log_history is a class attribute; keep this run's rounds separate.
        self.app.wrk.log_history = []
        self.slo_oracle = SLOMitigationOracle(problem=self)
        self.mitigation_oracle = ChaosImpactOracle(self, self.slo_oracle)
        self.alerts = FiringAlerts(self)

    @mark_fault_injected
    def inject_fault(self):
        self.slo_oracle.capture_baseline()
        self.alerts.capture_baseline()
        self.signals = telemetry.Signals(self.namespace, 120)
        self.signals.capture_baseline()

    def recover_fault(self):
        return
