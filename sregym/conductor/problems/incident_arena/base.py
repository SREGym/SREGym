"""Common structure for the problems ported from Incident Arena.

An Incident Arena incident is one or more independent *legs* (a revoked grant,
an undersized pool, a runtime toggle, ...) on one of three systems under test.
Each leg here owns its deploy-time configuration, injection, recovery, the
white-box checks that prove it was repaired safely, and any durability
challenge the Incident Arena verifier ran against it. A problem composes legs
and reads its ticket, load profile, health bands and answer key from the
vendored task contract (:mod:`sregym.conductor.problems.incident_arena.task`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sregym.conductor.oracles.incident_arena import CheckResult, IncidentArenaMitigationOracle, OutcomeSpec
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.incident_arena.task import IncidentArenaTask
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

Challenge = Callable[[], CheckResult]


class FaultLeg:
    """One independent cause of an incident (or a scope guard with no fault)."""

    #: (service, component) pair from the task's closed component registry.
    component: str = ""
    #: False for context the incident imposes but does not grade as a cause.
    is_cause: bool = True

    def __init__(self) -> None:
        self.problem: IncidentArenaProblem | None = None

    def bind(self, problem: IncidentArenaProblem) -> FaultLeg:
        self.problem = problem
        return self

    @property
    def app(self):
        return self.problem.app

    @property
    def namespace(self) -> str:
        return self.problem.namespace

    # Overridable hooks -------------------------------------------------------
    def deploy_values(self) -> dict[str, Any]:
        """Chart values the leg needs at install time (latent knobs, components)."""
        return {}

    def capture_baseline(self) -> None:
        """Record healthy state before injection (scope and restart-masking basis)."""

    def inject(self) -> None:
        """Break the system. Guards (legs without a fault) leave this a no-op."""

    def recover(self) -> None:
        """Undo :meth:`inject` so the cluster is reusable."""

    def checks(self, phase: str) -> list[CheckResult]:
        """White-box assertions for ``phase`` ('declaration' or 'soak_end')."""
        return []

    def challenges(self) -> list[Challenge]:
        """Verifier-owned durability challenges run after the declaration checks."""
        return []

    def describe(self) -> str:
        """Concrete injected mutation, for the diagnosis ground truth."""
        return ""


class IncidentArenaProblem(Problem):
    """Base for every Incident Arena problem; subclasses set the class attributes."""

    #: Directory under ``tasks/`` holding the vendored Incident Arena contract.
    TASK: str = ""
    #: SREGym problem id (registry key).
    PROBLEM_ID: str = ""
    #: Seconds of healthy traffic before injection / after injection.
    BASELINE_S: int = 120
    PROPAGATION_S: int = 120
    #: Re-base latency bands on the latency measured before injection. Off when
    #: the fault ships with the deployed release, so that window is not healthy.
    HEALTHY_BASELINE: bool = True

    def __init__(self):
        self.task = IncidentArenaTask.load(self.TASK)
        app = self.create_app()
        super().__init__(app=app)
        self.kubectl = app.kubectl
        self.problem_id = self.PROBLEM_ID
        self.baseline_duration_s = self.BASELINE_S
        self.propagation_duration_s = self.PROPAGATION_S

        self.legs: list[FaultLeg] = [leg.bind(self) for leg in self.build_legs()]
        self.guards: list[FaultLeg] = [guard.bind(self) for guard in self.build_guards()]

        name, profile = self.load_profile()
        app.set_load_profile(name, profile)
        for leg in [*self.legs, *self.guards]:
            app.configure(leg.deploy_values())
        app.configure(self.deploy_values())
        app.description = self.compose_description()

        self.root_cause = self.compose_root_cause()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = IncidentArenaMitigationOracle(problem=self)
        app.create_workload()

    # ------------------------------------------------------------------ subclass hooks
    def create_app(self):
        raise NotImplementedError

    def build_legs(self) -> list[FaultLeg]:
        raise NotImplementedError

    def build_guards(self) -> list[FaultLeg]:
        """App-wide scope guards (no injection), e.g. release image unchanged."""
        return []

    def deploy_values(self) -> dict[str, Any]:
        return {}

    def load_profile(self) -> tuple[str, dict[str, Any]]:
        return self.task.load_profile()

    #: Paragraph appended to every ticket of an app: the operating constraints
    #: Incident Arena enforced through its confined operator shell.
    GROUND_RULES: str = ""

    # ------------------------------------------------------------------ prompt + ground truth
    def compose_description(self) -> str:
        parts = [self.app.base_description.strip(), "An incident ticket has been filed for this deployment:"]
        parts.append("\n".join(f"> {line}" if line else ">" for line in self.task.ticket.splitlines()))
        if self.GROUND_RULES:
            parts.append(self.GROUND_RULES.strip())
        return "\n\n".join(parts)

    def compose_root_cause(self) -> str:
        findings = self.task.answer_key
        components = ", ".join(f"{f['service']}/{f['component']}" for f in findings)
        lines = [f"{len(findings)} independent root cause(s); a complete diagnosis names every one."]
        for index, finding in enumerate(findings, 1):
            mechanism = " ".join(str(finding.get("mechanism", "")).split())
            lines.append(f"{index}. {finding['service']} ({finding['component']}): {mechanism}")
        injected = [leg.describe() for leg in self.legs if leg.is_cause and leg.describe()]
        if injected:
            lines.append("Injected mutation(s): " + " ".join(injected))
        return self.build_structured_root_cause(
            component=components, namespace=self.namespace, description="\n".join(lines)
        )

    def outcome_spec(self) -> OutcomeSpec:
        return OutcomeSpec(
            thresholds=self.task.thresholds, gate_latency=self.task.gates_latency, soak_s=self.task.soak_s
        )

    # ------------------------------------------------------------------ fault lifecycle
    def capture_baseline(self) -> None:
        # Legs first: guards read what the legs resolve (e.g. the site account).
        for leg in [*self.legs, *self.guards]:
            leg.capture_baseline()

    @mark_fault_injected
    def inject_fault(self):
        for leg in self.legs:
            logger.info("[%s] injecting %s", self.problem_id, type(leg).__name__)
            leg.inject()
        return True

    def recovery_order(self) -> list[FaultLeg]:
        """Legs in the order ``recover_fault`` undoes them (reverse injection by default)."""
        return list(reversed(self.legs))

    @mark_fault_injected
    def recover_fault(self):
        errors = []
        for leg in self.recovery_order():
            try:
                leg.recover()
            except Exception as exc:  # keep recovering the remaining legs
                logger.exception("[%s] recovering %s failed", self.problem_id, type(leg).__name__)
                errors.append(f"{type(leg).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return True

    # ------------------------------------------------------------------ grading hooks
    def run_checks(self, phase: str) -> list[CheckResult]:
        results: list[CheckResult] = []
        for leg in [*self.legs, *self.guards]:
            try:
                results.extend(leg.checks(phase))
            except Exception as exc:
                logger.exception("[%s] %s check raised", self.problem_id, type(leg).__name__)
                results.append(
                    CheckResult(
                        f"{type(leg).__name__}_{phase}",
                        False,
                        reason="service_unhealthy",
                        detail={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )
        return results

    def challenges(self) -> list[Challenge]:
        out: list[Challenge] = []
        for leg in [*self.legs, *self.guards]:
            out.extend(leg.challenges())
        return out
