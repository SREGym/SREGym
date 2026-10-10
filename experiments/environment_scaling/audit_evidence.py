"""Opt-in checks for the historical environment-scaling experiment reports."""

import pathlib

import pytest

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


def test_the_committed_results_artifact_matches_the_registry():
    """The PR must not claim a problem is hard and leave it unregistered.

    `difficult-problems.json` is the evidence shipped with this branch, since the
    `results/` tree is gitignored and therefore not reviewable. Three committed
    results files already asserted stale pre-correction outcomes that a later
    screen contradicted, so the two have to be checked against each other.
    """
    import json

    artifact = json.loads(pathlib.Path("experiments/environment_scaling/evidence/difficult-problems.json").read_text())
    claimed = {p["problem"] for p in artifact["problems"]}
    assert claimed == SCREENED_DISCRIMINATING, (
        f"artifact and registry disagree: only in artifact {sorted(claimed - SCREENED_DISCRIMINATING)}, "
        f"only in registry {sorted(SCREENED_DISCRIMINATING - claimed)}"
    )
    registered = registered_problem_ids()
    assert claimed <= registered, f"claimed hard but unregistered: {sorted(claimed - registered)}"

    for problem in artifact["problems"]:
        # A problem is only in here because an agent failed it at least once.
        assert problem["failures"] >= 1, f"{problem['problem']} has no recorded failure"
        assert problem["valid_attempts"] >= 2, f"{problem['problem']} rests on fewer than two valid attempts"
        assert problem["difficulty"] > 0
        # Confidence has to be stated, and a single failure is never "solid".
        assert problem["confidence"] in ("solid", "weak")
        if problem["failures"] < 2:
            assert problem["confidence"] == "weak", f"{problem['problem']} has one failure and must not be called solid"


def test_superseded_results_files_say_so():
    """A stale status in a committed file is a claim the PR is making."""
    import json

    for name in (
        "gitlab-notification-intermittent-results.json",
        "gitlab-notification-delayed-audit-results.json",
        "stripe-config-screen.json",
    ):
        d = json.loads((pathlib.Path("experiments/environment_scaling/evidence") / name).read_text())
        assert d.get("superseded_by"), f"{name} carries a pre-correction status without saying so"
        assert "SUPERSEDED" in d["status"]
        assert d.get("original_status"), f"{name} should keep what it originally claimed"
