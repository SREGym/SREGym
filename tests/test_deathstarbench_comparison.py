import pathlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from experiments.environment_scaling.evaluate import (
    AGENT_CLIENT_SOURCES,
    APPLICATIONS,
    SUPPORTED_TIERS,
    prepare_agent_registry,
    problem_id,
    summarize,
)


def test_cli_version_override_is_private_and_does_not_mutate_the_original_registry(tmp_path, monkeypatch):
    import yaml

    original = tmp_path / "custom-agents.yaml"
    content = {"agents": [{"name": "codex", "agent_version": None, "kickoff_env": {"SETTING": "keep"}}]}
    original.write_text(yaml.safe_dump(content))
    monkeypatch.setenv("SREGYM_AGENT_REGISTRY", str(original))
    output = tmp_path / "results"
    output.mkdir()
    pinned = prepare_agent_registry(tmp_path, output, "0.157.0", "codex")
    assert yaml.safe_load(original.read_text()) == content
    new = yaml.safe_load(pinned.read_text())["agents"][0]
    assert new["agent_version"] == "0.157.0"
    assert new["kickoff_env"] == {"SETTING": "keep"}
    assert pinned.stat().st_mode & 0o777 == 0o600


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


def test_registry_pins_the_selected_agent_not_always_codex(tmp_path, monkeypatch):
    import yaml

    original = tmp_path / "custom-agents.yaml"
    original.write_text(
        yaml.safe_dump(
            {"agents": [{"name": "codex", "agent_version": None}, {"name": "claudecode", "agent_version": None}]}
        )
    )
    monkeypatch.setenv("SREGYM_AGENT_REGISTRY", str(original))
    output = tmp_path / "results"
    output.mkdir()
    pinned = yaml.safe_load(prepare_agent_registry(tmp_path, output, "2.1.0", "claudecode").read_text())
    versions = {entry["name"]: entry["agent_version"] for entry in pinned["agents"]}
    assert versions == {"claudecode": "2.1.0", "codex": None}


def test_a_missing_agent_registration_is_rejected_by_name(tmp_path, monkeypatch):
    import yaml

    original = tmp_path / "custom-agents.yaml"
    original.write_text(yaml.safe_dump({"agents": [{"name": "codex", "agent_version": None}]}))
    monkeypatch.setenv("SREGYM_AGENT_REGISTRY", str(original))
    output = tmp_path / "results"
    output.mkdir()
    with pytest.raises(ValueError, match="claudecode"):
        prepare_agent_registry(tmp_path, output, "2.1.0", "claudecode")


def test_every_selectable_agent_has_its_driver_and_helper_packages():
    """A missing helper package only fails inside the container, mid-attempt."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for agent, names in AGENT_CLIENT_SOURCES.items():
        assert (root / "clients" / agent / "driver.py").is_file(), agent
        assert agent in names, f"{agent} must ship its own client package"
        for name in names:
            assert (root / "clients" / name).is_dir(), (agent, name)


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


def test_unregistered_variant_is_rejected_before_any_run_or_image_build(tmp_path, monkeypatch):
    from experiments.environment_scaling import evaluate

    monkeypatch.setattr(
        "sregym.conductor.problems.registry.ProblemRegistry",
        lambda: SimpleNamespace(PROBLEM_REGISTRY={}),
    )
    build = Mock()
    run = Mock()
    monkeypatch.setattr(evaluate, "prepare_agent_image", build)
    monkeypatch.setattr(evaluate.subprocess, "run", run)
    output = tmp_path / "not-created"
    monkeypatch.setattr("sys.argv", ["evaluate", "--model", "test", "--output", str(output)])
    with pytest.raises(SystemExit) as exit:
        evaluate.main()
    assert exit.value.code == 2
    build.assert_not_called()
    run.assert_not_called()
    assert not output.exists()
