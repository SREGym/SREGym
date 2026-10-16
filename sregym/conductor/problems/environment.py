"""Shared application and grading setup for environment fault ports."""

from __future__ import annotations

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.workload_health import LoadgenHealthOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.app_registry import AppRegistry
from sregym.service.kubectl import KubeCtl

LOAD_PROFILES = {
    "frappe": ("lite_frappe", {"base": "frappe_jobs", "soak_cycles": 3}),
    "saleor": ("lite_saleor", {"base": "saleor_eval", "soak_cycles": 4}),
    "slack_spine": ("lite_slack", {"base": "slack_session", "soak_cycles": 2}),
}


DEPLOY_VALUES = {
    "frappe": {
        "erpnext": {
            "nginx": {"defaultTopologySpread": {"whenUnsatisfiable": "ScheduleAnyway"}},
            "worker": {"defaultTopologySpread": {"whenUnsatisfiable": "ScheduleAnyway"}},
        }
    },
}


BASELINE_S = 120


PROPAGATION_S = 60


def make_app(app_name: str):
    """Build an Incident Arena app with its continuous, fault-free load profile."""
    if app_name not in LOAD_PROFILES:
        raise ValueError(f"Unsupported load profile: {app_name} (expected one of {sorted(LOAD_PROFILES)})")
    app = AppRegistry().get_app_instance(app_name)
    app.set_load_profile(*LOAD_PROFILES[app_name])
    app.configure(DEPLOY_VALUES.get(app_name, {}))
    return app


def with_app_health(problem, oracle: Oracle, *, health: Oracle | None = None) -> CompoundedOracle:
    """Require ``oracle`` (the fault's own check) and recovered user traffic."""
    compound = CompoundedOracle(
        problem, fault=oracle, app_health=health if health is not None else LoadgenHealthOracle(problem)
    )
    # Several fault oracles wait minutes for rollouts or replay a trigger, and
    # the health window adds SOAK_S + WINDOW_S on top.
    compound.evaluation_timeout_seconds = max(1200.0, oracle.evaluation_timeout_seconds or 0.0)
    return compound


def configure_port(
    problem: Problem,
    app_name: str,
    *,
    component: str,
    description: str,
    oracle_factory,
    root_cause_namespace: str | None = None,
    health_oracle_factory=None,
) -> None:
    """Configure grading and timing after the fault constructor initializes its state."""
    problem.app_name = app_name
    problem.kubectl = KubeCtl()
    problem.baseline_duration_s = BASELINE_S
    problem.propagation_duration_s = PROPAGATION_S
    problem.root_cause = problem.build_structured_root_cause(
        component=component,
        namespace=problem.namespace if root_cause_namespace is None else root_cause_namespace,
        description=description,
    )
    problem.diagnosis_oracle = LLMAsAJudgeOracle(problem=problem, expected=problem.root_cause)
    health = health_oracle_factory(problem) if health_oracle_factory is not None else None
    problem.mitigation_oracle = with_app_health(problem, oracle_factory(problem), health=health)
    problem.app.create_workload()


def reopen_connections(problem: Problem, deployment: str, timeout_s: int = 300) -> None:
    """Roll ``deployment`` so the connections through the faulted hop are re-opened under the fault.

    Slack Spine's clients (undici ``fetch`` in the svc-* roles, aiohttp in the
    load generator) keep HTTP connections alive, and established connections
    survive Service selector, CoreDNS and NetworkPolicy changes. Without a
    restart those faults reach users only as connections happen to recycle; a
    routine rollout of a deployment on the path makes them bite immediately.
    """
    problem.kubectl.exec_command_checked(f"kubectl rollout restart deployment/{deployment} -n {problem.namespace}")
    problem.kubectl.exec_command_checked(
        f"kubectl rollout status deployment/{deployment} -n {problem.namespace} --timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )
