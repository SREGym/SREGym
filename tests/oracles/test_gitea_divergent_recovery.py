"""Grading for the Gitea divergence, especially the receipt/snapshot translation.

API receipts carry `repository` as `owner/name` and `number`; snapshot rows carry
`repo_id` and `index`. Every comparison the oracle makes crosses that boundary,
so a mistranslation would not raise -- it would silently report that every
acknowledged write is missing, and the problem would look impossible rather than
broken.
"""

from sregym.conductor.oracles.gitea_divergent_recovery import GiteaDivergentRecoveryOracle, gitea_keys
from sregym.conductor.oracles.gitlab_divergent_recovery import divergence_report

REPOSITORIES = {"octo/alpha": 1, "octo/beta": 2}


def receipt(repository, number, title, body="body"):
    return {"repository": repository, "number": number, "title": title, "body": body}


def row(repo_id, index, name, content="body"):
    return {"repo_id": repo_id, "index": index, "name": name, "content": content}


def report(shared, orphaned, post, issues):
    return divergence_report(shared, orphaned, post, issues, keys=gitea_keys(REPOSITORIES))


def test_receipts_and_snapshot_rows_describe_the_same_issue():
    content, identity, label = gitea_keys(REPOSITORIES)
    assert content(receipt("octo/alpha", 7, "t")) == content(row(1, 7, "t"))
    assert identity(receipt("octo/alpha", 7, "t")) == identity(row(1, 7, "t"))
    assert label(receipt("octo/beta", 1, "a title")) == "a title"
    assert label(row(2, 1, "a title")) == "a title"


def test_a_complete_recovery_retains_both_histories():
    shared = [row(1, 1, "shared")]
    orphaned = [receipt("octo/alpha", 2, "orphan")]
    post = [receipt("octo/alpha", 2, "post")]
    # The orphan was re-accepted with a fresh number; the post-promotion issue
    # kept number 2. Both are present, so nothing is reported.
    issues = [row(1, 1, "shared"), row(1, 2, "post"), row(1, 3, "orphan")]
    result = report(shared, orphaned, post, issues)
    assert GiteaDivergentRecoveryOracle.verdict(result) is None
    assert result["orphaned_recovered"] == 1


def test_failing_back_is_named_as_lost_post_promotion_writes():
    """Restoring the demoted primary looks healthy and loses the newer writes."""
    shared = [row(1, 1, "shared")]
    orphaned = [receipt("octo/alpha", 2, "orphan")]
    post = [receipt("octo/alpha", 2, "post")]
    issues = [row(1, 1, "shared"), row(1, 2, "orphan")]  # the east snapshot, restored
    verdict = GiteaDivergentRecoveryOracle.verdict(report(shared, orphaned, post, issues))
    assert verdict["reason"] == "post_promotion_writes_lost"


def test_doing_nothing_is_named_as_abandoned_orphans():
    shared = [row(1, 1, "shared")]
    orphaned = [receipt("octo/alpha", 2, "orphan")]
    post = [receipt("octo/alpha", 2, "post")]
    issues = [row(1, 1, "shared"), row(1, 2, "post")]  # untouched promoted history
    verdict = GiteaDivergentRecoveryOracle.verdict(report(shared, orphaned, post, issues))
    assert verdict["reason"] == "orphaned_writes_abandoned"


def test_re_accepting_an_orphan_twice_is_named_as_duplication():
    shared = []
    orphaned = [receipt("octo/alpha", 2, "orphan")]
    post = []
    issues = [row(1, 3, "orphan"), row(1, 4, "orphan")]
    verdict = GiteaDivergentRecoveryOracle.verdict(report(shared, orphaned, post, issues))
    assert verdict["reason"] == "acknowledged_writes_duplicated"


def test_issues_in_different_repositories_do_not_collide():
    """Numbers are per repository, so alpha#1 and beta#1 are different issues."""
    shared = [row(1, 1, "alpha one"), row(2, 1, "beta one")]
    issues = [row(1, 1, "alpha one"), row(2, 1, "beta one")]
    assert GiteaDivergentRecoveryOracle.verdict(report(shared, [], [], issues)) is None
    swapped = [row(1, 1, "beta one"), row(2, 1, "alpha one")]
    assert GiteaDivergentRecoveryOracle.verdict(report(shared, [], [], swapped))["reason"] == "shared_history_changed"


def test_every_named_reason_is_classified_as_an_agent_error():
    from sregym.conductor.oracles.failure import FailureClass

    classes = GiteaDivergentRecoveryOracle._failure_classes()
    for reason in (
        "database_schema_missing",
        "post_promotion_writes_lost",
        "orphaned_writes_abandoned",
        "acknowledged_writes_duplicated",
        "shared_history_changed",
        "recovery_evidence_changed",
        "acknowledged_git_data_changed",
    ):
        assert classes[reason] is FailureClass.AGENT_ERROR
