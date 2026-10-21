"""Script-defined problems with SLO-based mitigation grading.

``SLOProblem`` runs a fault artifact (see ``sregym/chaos/artifact.py``)
through the conductor. Bundles with diagnosis metadata use the ordinary LLM
diagnosis judge. Mitigation uses the SLO measured by the workload generator
and application recovery checks, not an LLM judge.

Every injection also records the validity-gate measurements (impact,
observability, integrity) in ``results/chaos_gates/``, so each fixer attempt
can be checked to have faced a live fault.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from sregym.chaos import protected, telemetry
from sregym.chaos.artifact import FaultArtifact, FaultWorkspace
from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.apps.hotel_reservation.slo_recovery import HotelReservationRecoveryCheck
from sregym.conductor.oracles.apps.social_network.slo_recovery import SocialNetworkRecoveryCheck
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.slo_oracle import SLOMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.utils.decorators import mark_fault_injected


def make_app(name: str):
    if name == "hotel_reservation":
        from sregym.service.apps.hotel_reservation import HotelReservation

        return HotelReservation()
    if name == "social_network":
        from sregym.service.apps.social_network import SocialNetwork

        return SocialNetwork()
    raise ValueError(f"unsupported app {name!r}")


def gate_report_dir() -> Path:
    return Path(os.environ.get("SREGYM_CHAOS_REPORT_DIR", "results/chaos_gates"))


class FiringAlerts:
    """Prometheus alerts in the app namespace that were not firing at baseline."""

    def __init__(self, problem):
        self._oracle = AlertOracle(problem)
        self.namespace = problem.namespace
        self.baseline: set = set()

    def _instances(self) -> list[dict]:
        try:
            return self._oracle._query_firing_alerts(self.namespace)
        except Exception:
            return []

    @staticmethod
    def _key(alert: dict):
        return tuple(sorted((alert.get("labels") or {}).items()))

    def capture_baseline(self) -> None:
        self.baseline = {self._key(a) for a in self._instances()}

    def new(self) -> list[str]:
        return sorted(
            {
                (a.get("labels") or {}).get("alertname", "?")
                for a in self._instances()
                if self._key(a) not in self.baseline
            }
        )


class SLOProblem(Problem):
    # Seconds of post-injection workload used to judge impact; it doubles as
    # the propagation window before the agent starts.
    impact_window_s = 150

    def __init__(self, artifact: FaultArtifact):
        problems = artifact.problems()
        if problems:
            raise ValueError(f"Invalid fault artifact {artifact.path}: {'; '.join(problems)}")
        self.artifact = artifact
        self.workspace: FaultWorkspace | None = None
        super().__init__(app=make_app(artifact.app))
        self.problem_id = artifact.problem_id
        self.faulty_service = artifact.meta.get("affected_components", [])
        diagnosis = artifact.diagnosis
        if diagnosis is not None:
            self.root_cause = self.build_structured_root_cause(
                component=diagnosis["component"],
                namespace=self.namespace,
                description=diagnosis["description"],
            )
            self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        else:
            # Preserve script-only external bundles that have no diagnosis task.
            self.root_cause = artifact.meta.get("root_cause_class", "")
        # wrk2 needs time to calibrate before it records a latency histogram.
        # Keep the ordinary workload defaults unchanged for existing problems.
        self.app.create_workload(duration=30)
        # StreamWorkloadManager.log_history is a class attribute; keep this run's rounds separate.
        self.app.wrk.log_history = []
        recovery_check = {
            "hotel_reservation": HotelReservationRecoveryCheck,
            "social_network": SocialNetworkRecoveryCheck,
        }[artifact.app](self.app)
        self.mitigation_oracle = SLOMitigationOracle(
            problem=self, recurrence_bound_s=artifact.recurrence_bound_s, recovery_check=recovery_check
        )
        self.alerts = FiringAlerts(self)
        self.gates: dict = {}

    def _write_report(self, name: str, payload: dict) -> None:
        out = gate_report_dir()
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (out / f"{self.problem_id}.{name}.{stamp}.json").write_text(json.dumps(payload, indent=2, default=str))

    @mark_fault_injected
    def inject_fault(self):
        oracle = self.mitigation_oracle
        if oracle.slo is None:
            oracle.capture_baseline()
        self.alerts.capture_baseline()
        signals = telemetry.Signals(self.namespace, self.impact_window_s)
        signals.capture_baseline()
        before = protected.snapshot(self.namespace)
        leaks_before = protected.leak_scan(self.namespace)

        if self.workspace is None:
            self.workspace = self.artifact.workspace()
        result = self.workspace.run("inject.sh", self.namespace)
        injected_at = time.time()
        self.gates = {
            "problem_id": self.problem_id,
            "inject_returncode": result.returncode,
            "inject_stdout": result.stdout[-4000:],
            "inject_stderr": result.stderr[-4000:],
            "baseline": oracle.baseline_verdict.to_dict() if oracle.baseline_verdict else None,
            "slo": oracle.slo.to_dict(),
        }
        if result.returncode != 0:
            self._write_report("inject", self.gates)
            raise RuntimeError(f"inject.sh failed ({result.returncode}): {result.stderr[-500:]}")

        impact = oracle.measure(self.impact_window_s)
        self.gates.update(
            impact=impact.to_dict(),
            impact_pass=impact.violated,
            new_alerts=self.alerts.new(),
            **signals.changes(),
            integrity=protected.compare(before, protected.snapshot(self.namespace)),
            leaks=sorted(
                f"{where}: {text}"
                for text, where in protected.leak_scan(self.namespace).items()
                if text not in leaks_before
            ),
            workload_running=protected.workload_running(self.namespace),
            seconds_after_inject=round(time.time() - injected_at),
        )
        # Validation runs also check the fault does not heal itself.
        persistence_s = int(os.environ.get("CHAOS_PERSISTENCE_S", "0"))
        if persistence_s > 0:
            time.sleep(persistence_s)
            later = oracle.measure(120)
            self.gates.update(persistence=later.to_dict(), persistence_pass=later.violated)
            self.gates["new_alerts"] = sorted(set(self.gates["new_alerts"]) | set(self.alerts.new()))
        # Request failures and latency are observable without a Prometheus alert.
        self.gates["request_symptoms"] = sorted(
            {reason for detail in impact.details for reason in detail["violations"]}
        )
        self.gates["observability_pass"] = bool(
            self.gates["new_alerts"]
            or self.gates["log_surges"]
            or self.gates["cpu_surges"]
            or self.gates["request_symptoms"]
        )
        self.gates["integrity_pass"] = self.gates["integrity"]["intact"] and self.gates["workload_running"]
        self.gates["no_leak_pass"] = not self.gates["leaks"]
        self._write_report("inject", self.gates)
        print(
            f"[GATES] impact={impact.violated} ({impact.violating}/{impact.rounds} rounds) "
            f"alerts={self.gates['new_alerts']} integrity={self.gates['integrity_pass']}"
        )
        required = ["impact_pass", "observability_pass", "integrity_pass", "no_leak_pass"]
        if persistence_s > 0:
            required.append("persistence_pass")
        failed = [name for name in required if not self.gates[name]]
        if failed:
            raise RuntimeError(f"Fault injection validation failed: {', '.join(failed)}")

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        if self.workspace is None:
            return
        result = self.workspace.run("recover.sh", self.namespace)
        if result.returncode != 0:
            raise RuntimeError(f"recover.sh failed ({result.returncode}): {result.stderr[-500:]}")
        self.workspace.close()
        self.workspace = None


def slo_problems() -> dict[str, Callable[[], SLOProblem]]:
    """Registry entries for every artifact under the configured roots."""
    from sregym.chaos.artifact import discover_faults

    return {problem_id: partial(SLOProblem, artifact) for problem_id, artifact in discover_faults().items()}
