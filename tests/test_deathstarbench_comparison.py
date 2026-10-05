import pathlib

import pytest

from scripts.evaluate_deathstarbench import (
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

    source = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "evaluate_deathstarbench.py"
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


#: Problems from this work that a screen showed to separate a frontier agent,
#: and are therefore registered. Each entry is the exact id that was screened.
SCREENED_DISCRIMINATING = {
    "gitlab_notification_delayed_audit_replicated",
    "gitlab_notification_intermittent_replicated",
    "stripe_feature_config_single",
    # 2 of 3, with its failure in the same graded mode as the two races. Weak at
    # n=3; registered on the same standard as stripe_feature_config.
    "gitlab_notification_unannounced_ambiguity_replicated",
}

#: Registered only so a first screen can resolve them. A problem that is not in
#: the registry cannot be validated, so anything awaiting its first screen has to
#: be registered; the rule is that a 3-of-3 result then takes it out again.
CANDIDATES_AWAITING_A_FIRST_SCREEN: set[str] = set()

#: Built, tested, and deliberately NOT registered: every one was solved 3 of 3
#: under a generic task description, so registering them spends campaign time
#: without discriminating. The modules must keep importing and constructing --
#: four of these are superclasses of the registered notification problems.
BUILT_BUT_GATED = {
    "sregym.conductor.problems.gitea_compound_loss": "GiteaCompoundLoss",
    "sregym.conductor.problems.gitea_regional_failover": "GiteaRegionalFailover",
    "sregym.conductor.problems.gitlab_notification_unannounced": "GitLabNotificationUnannounced",
    "sregym.conductor.problems.unannounced_families": "MattermostCapacityCascadeUnannounced",
    "sregym.conductor.problems.gitea_database_deletion": "GiteaDatabaseDeletion",
    "sregym.conductor.problems.gitlab_database_deletion": "GitLabDatabaseDeletion",
    "sregym.conductor.problems.gitlab_notification_recovery": "GitLabNotificationRecovery",
    "sregym.conductor.problems.gitlab_notification_ambiguity": "GitLabNotificationAmbiguity",
    "sregym.conductor.problems.mattermost_capacity_cascade": "MattermostCapacityCascade",
    "sregym.conductor.problems.coordination_collapse": "CoordinationCollapse",
}


def registered_problem_ids():
    import ast

    source = pathlib.Path("sregym/conductor/problems/registry.py").read_text()
    # Read the ids statically: constructing the registry needs a live cluster.
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_only_the_problems_a_screen_separated_on_are_registered():
    """The registry is gated on evidence, not on what happens to be built.

    Every family in this branch is a working problem with a passing lifecycle,
    but six of them were solved 3 of 3 by Codex `gpt-6-astra` under a generic
    task description. Registering those spends campaign budget to re-learn that
    they are saturated. The gate is recorded here so that re-registering one is
    a deliberate edit with a reason attached, and so that deleting a registered
    problem cannot pass silently either.
    """
    registered = registered_problem_ids()
    # Candidates awaiting a first screen are allowed to be registered: a problem
    # cannot be screened unless the campaign runner can resolve it.
    missing = SCREENED_DISCRIMINATING - registered
    assert not missing, f"screened discriminating problems went unregistered: {sorted(missing)}"

    saturated = {
        "gitea_database_deletion_single",
        "gitlab_database_deletion_single",
        "gitlab_notification_recovery_replicated",
        "gitlab_notification_ambiguity_replicated",
        "mattermost_capacity_cascade_single",
        "coordination_collapse_single",
        # Candidates screened and found not to discriminate. Their code stays in
        # the tree -- the negative results are the useful part -- but a screen
        # has to justify registering one.
        "gitlab_notification_unannounced_replicated",
        "mattermost_capacity_cascade_unannounced_single",
        "gitea_compound_loss_single",
        "gitea_regional_failover_single",
        "gitea_database_deletion_unannounced_single",
        "gitlab_database_deletion_unannounced_single",
        "coordination_lagging_ack_single",
        "coordination_lagging_admission_single",
        # 10 of 10 at n=10, the strongest saturation evidence in the set.
        "gitlab_regional_failover_single",
    } - CANDIDATES_AWAITING_A_FIRST_SCREEN
    regressed = saturated & registered
    assert not regressed, (
        f"these were solved 3 of 3 and should stay gated until a screen says otherwise: {sorted(regressed)}"
    )


@pytest.mark.parametrize(("module_name", "class_name"), sorted(BUILT_BUT_GATED.items()))
def test_gated_families_still_import_and_are_real_problems(module_name, class_name):
    """Gating must not rot the code: these are superclasses of what is registered."""
    import importlib

    from sregym.conductor.problems.base import Problem

    problem = getattr(importlib.import_module(module_name), class_name)
    assert issubclass(problem, Problem)
    # The lifecycle contract, rather than `application_class`: some families
    # declare that attribute and some build their application in __init__.
    for method in ("inject_fault", "recover_fault"):
        assert callable(getattr(problem, method)), f"{class_name} has no {method}"
    assert "scale_tier" in problem.__init__.__code__.co_varnames
