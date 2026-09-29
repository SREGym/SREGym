import pathlib

import pytest

from scripts.evaluate_deathstarbench import (
    AGENT_CLIENT_SOURCES,
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
