"""Mitigation oracle that grades the end-user SLO measured by the workload generator."""

from __future__ import annotations

from sregym.chaos import protected, slo
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass


class SLOMitigationOracle(Oracle):
    """Passes when the workload meets the SLO over a sustained window.

    A transient recovery does not count: after a short buffer the oracle
    collects ``sustained_seconds`` of fresh wrk2 rounds and requires them to be
    healthy, tolerating only isolated, non-adjacent noisy rounds (``slo.judge``). The SLO is calibrated against rounds observed before
    the fault was injected.
    """

    importance = 1.0
    evaluation_timeout_seconds = 900
    FAILURE_CLASSES = {
        "slo_violated": FailureClass.AGENT_ERROR,
        "slo_no_rounds": FailureClass.AMBIGUOUS,
        "slo_incomplete_measurement": FailureClass.AMBIGUOUS,
        "protected_component_modified": FailureClass.AGENT_ERROR,
    }

    def __init__(
        self,
        problem,
        sustained_seconds: int = 180,
        buffer_seconds: int = 30,
        min_rounds: int = 4,
        recurrence_bound_s: int = 0,
    ):
        super().__init__(problem)
        # Observe the next natural recurrence plus complete workload rounds.
        # Never reinject the fault into a correctly repaired application.
        round_s = slo.round_seconds(problem.app)
        self.sustained_seconds = max(sustained_seconds, recurrence_bound_s + 3 * round_s if recurrence_bound_s else 0)
        self.buffer_seconds = buffer_seconds
        self.min_rounds = min_rounds
        self.evaluation_timeout_seconds = max(900, buffer_seconds + self.sustained_seconds + 3 * round_s + 120)
        self.slo: slo.SLO | None = None
        self.baseline_verdict: slo.WindowVerdict | None = None
        self.protected_before: dict[str, str] | None = None

    def capture_baseline(self) -> None:
        app = self.problem.app
        policy = slo.SLO(target_rps=slo.target_rps(app))
        rounds = slo.collect_rounds(app, window_s=90, min_rounds=3)
        baseline = slo.judge(policy, rounds)
        if len(rounds) < 3 or not baseline.measurement_complete:
            raise RuntimeError("SLO baseline requires at least three complete rounds with latency measurements")
        if not baseline.healthy:
            raise RuntimeError(f"Application does not meet the baseline SLO: {baseline.to_dict()}")
        p99s = sorted(r.p99_ms for r in rounds if r.p99_ms is not None)
        median = len(rounds) // 2
        policy.baseline_p99_ms = p99s[median]
        policy.baseline_timeout_ratio = sorted(r.timeout_ratio for r in rounds)[median]
        policy.baseline_error_ratio = sorted(r.error_ratio for r in rounds)[median]
        self.slo = policy
        self.baseline_verdict = baseline
        self.protected_before = protected.snapshot(app.namespace)
        print(
            f"[SLO] baseline {self.baseline_verdict.violating}/{self.baseline_verdict.rounds} rounds violating; "
            f"slo={self.slo.to_dict()}"
        )

    def measure(self, window_s: int) -> slo.WindowVerdict:
        rounds = slo.collect_rounds(self.problem.app, window_s=window_s, min_rounds=self.min_rounds)
        return slo.judge(self.slo, rounds)

    def evaluate(self) -> dict:
        if self.slo is None:
            self.capture_baseline()
        import time

        time.sleep(self.buffer_seconds)
        verdict = self.measure(self.sustained_seconds)
        integrity = protected.compare(self.protected_before or {}, protected.snapshot(self.problem.app.namespace))
        detail = {"slo": self.slo.to_dict(), "window": verdict.to_dict(), "integrity": integrity}
        if not integrity["intact"]:
            return self.fail("protected_component_modified", **detail)
        if verdict.rounds < self.min_rounds:
            return self.fail("slo_no_rounds", **detail)
        if not verdict.measurement_complete and not verdict.violated:
            return self.fail("slo_incomplete_measurement", **detail)
        if not verdict.healthy:
            print(f"[❌] SLO violated in {verdict.violating}/{verdict.rounds} rounds")
            return self.fail("slo_violated", **detail)
        print(
            f"[✅] SLO met over {self.sustained_seconds}s ({verdict.violating}/{verdict.rounds} isolated noisy rounds)"
        )
        return {"success": True, "detail": detail}
