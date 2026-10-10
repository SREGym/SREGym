import pathlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from experiments.environment_scaling.evaluate import (
    AGENTS,
    APPLICATIONS,
    SUPPORTED_TIERS,
    problem_id,
    summarize,
)


def test_database_deletion_only_runs_on_supported_gitea_tiers():
    assert problem_id("gitea", "replicated", "database_deletion") == "gitea_database_deletion_replicated"
    with pytest.raises(ValueError):
        problem_id("hotel_reservation", "single", "database_deletion")
    with pytest.raises(ValueError):
        problem_id("gitea", "expanded", "database_deletion")


def test_infrastructure_failure_is_inconclusive_not_increased_difficulty():
    result = summarize([{"run_status": "incomplete", "deploy_failed": "True", "Mitigation.success": "False"}], 3)
    assert result["complete"] == 0
    assert result["difficulty"] is None
    assert result["inconclusive"]


def test_difficulty_uses_only_completed_graded_attempts():
    result = summarize(
        [
            {"run_status": "complete", "Mitigation.success": s, "Mitigation.failure_class": "agent_error"}
            for s in ("True", "False", "True")
        ],
        3,
    )
    assert result["successes"] == 2
    assert not result["inconclusive"]
    assert abs(result["difficulty"] - 1 / 3) < 0.00001


def test_legacy_and_scaled_tasks_are_distinct():
    assert problem_id("hotel_reservation", "legacy") == "wrong_service_selector_hotel_reservation"
    assert problem_id("hotel_reservation", "replicated").endswith("hotel_reservation_replicated")


def test_environment_failure_with_a_grade_is_excluded():
    result = summarize(
        [{"run_status": "complete", "Mitigation.success": "False", "Mitigation.failure_class": "environment_error"}], 1
    )
    assert result["complete"] == 0
    assert result["inconclusive"]


def test_ambiguous_failures_cannot_establish_model_difficulty():
    result = summarize(
        [{"run_status": "complete", "Mitigation.success": "False", "Mitigation.failure_class": "ambiguous"}], 1
    )
    assert result["ambiguous_failures"] == 1
    assert result["inconclusive"]


def test_every_selectable_agent_has_a_driver():
    root = pathlib.Path(__file__).resolve().parent.parent
    for agent in AGENTS:
        assert (root / "clients" / agent / "driver.py").is_file(), agent


def test_every_incident_the_cli_offers_can_be_routed():
    """The CLI's `--incident` choices and `problem_id` routing must agree.

    They did not: both new families were routable but absent from the choices,
    so `--incident capacity_cascade` died in argument parsing before any work.
    """
    import ast

    source = pathlib.Path(__file__).resolve().parent.parent / "experiments" / "environment_scaling" / "evaluate.py"
    tree = ast.parse(source.read_text())
    choices = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [a for a in node.args if isinstance(a, ast.Constant) and a.value == "--incident"]
        if not args:
            continue
        for keyword in node.keywords:
            if keyword.arg == "choices":
                choices = [e.value for e in keyword.value.elts]
    assert choices, "could not find the --incident choices"
    assert {"regional_failover", "capacity_cascade"} <= set(choices)

    # Every offered incident must route for at least one application and tier.
    for incident in choices:
        routed = []
        for app in APPLICATIONS:
            for tier in SUPPORTED_TIERS[app]:
                try:
                    routed.append(problem_id(app, tier, incident))
                except ValueError:
                    continue
        assert routed, f"{incident} is selectable but routes nowhere"


def test_default_comparison_runs_only_registered_legacy_tasks(tmp_path, monkeypatch):
    from experiments.environment_scaling import evaluate

    ids = {f"wrong_service_selector_{app}" for app in evaluate.DEFAULT_APPLICATIONS}
    monkeypatch.setattr(
        "sregym.conductor.problems.registry.ProblemRegistry",
        lambda: SimpleNamespace(PROBLEM_REGISTRY=dict.fromkeys(ids)),
    )
    calls = []

    def validate(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(evaluate.subprocess, "run", validate)
    monkeypatch.setattr("sys.argv", ["evaluate", "--model", "test", "--validate-only", "--output", str(tmp_path)])
    assert evaluate.main() == 0
    assert {call[call.index("--problem") + 1] for call in calls} == ids


def test_unregistered_variant_is_rejected_before_any_run(tmp_path, monkeypatch):
    from experiments.environment_scaling import evaluate

    monkeypatch.setattr(
        "sregym.conductor.problems.registry.ProblemRegistry",
        lambda: SimpleNamespace(PROBLEM_REGISTRY={}),
    )
    run = Mock()
    monkeypatch.setattr(evaluate.subprocess, "run", run)
    output = tmp_path / "not-created"
    monkeypatch.setattr("sys.argv", ["evaluate", "--model", "test", "--output", str(output)])
    with pytest.raises(SystemExit) as exit:
        evaluate.main()
    assert exit.value.code == 2
    run.assert_not_called()
    assert not output.exists()


@pytest.mark.parametrize("agent", AGENTS)
def test_experiment_uses_normal_benchmark_arguments(tmp_path, monkeypatch, agent):
    import json

    from experiments.environment_scaling import evaluate

    pid = "gitlab_notification_delayed_audit_replicated"
    monkeypatch.setattr(evaluate, "BASE_PARENT_DIR", tmp_path)
    monkeypatch.setattr(
        "sregym.conductor.problems.registry.ProblemRegistry",
        lambda: SimpleNamespace(PROBLEM_REGISTRY={pid: None}),
    )
    driver = tmp_path / "clients" / agent / "driver.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("# dummy driver for the report digest\n")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "main.py":
            directory = tmp_path / "results" / "campaign" / agent / pid
            directory.mkdir(parents=True)
            (directory / f"{pid}_{agent}_results.csv").write_text(
                "run_status,Mitigation.success,Mitigation.failure_class\ncomplete,True,\n"
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(evaluate.subprocess, "run", run)
    output = tmp_path / "comparison"
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate",
            "--model",
            "test-model",
            "--agent",
            agent,
            "--attempts",
            "1",
            "--applications",
            "gitlab_ce",
            "--tiers",
            "replicated",
            "--incident",
            "notification_delayed_audit",
            "--output",
            str(output),
        ],
    )
    assert evaluate.main() == 0
    command, kwargs = calls[1]
    assert command[1] == "main.py"
    assert command[command.index("--agent") + 1] == agent
    assert command[command.index("--problem") + 1] == pid
    assert not {"--agent-image", "--agent-version", "--stages"}.intersection(command)
    assert "env" not in kwargs
    report = json.loads((output / "comparison.json").read_text())
    assert report["stages"] == ["diagnosis", "mitigation"]
    assert report["results"][pid]["successes"] == 1
