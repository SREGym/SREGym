"""The grading contract for two divergent acknowledged histories.

The point of this family is that no single restore recovers it. These tests pin
that down: failing back and doing nothing each fail, for their own reason.
"""

import pytest

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.gitlab_divergent_recovery import (
    GitLabDivergentRecoveryOracle,
    both_histories_retained,
    content_key,
    divergence_report,
)

PROJECT = 7


def issue(iid, title, *, project=PROJECT, confidential=False):
    return {
        "project_id": project,
        "iid": iid,
        "title": title,
        "description": "body of " + title,
        "confidential": confidential,
        "author_id": 1,
        "state_id": 1,
    }


def receipt(iid, title, *, project=PROJECT, confidential=False):
    return {
        "project": project,
        "iid": iid,
        "title": title,
        "description": "body of " + title,
        "confidential": confidential,
    }


#: The history both regions agreed on before the partition.
SHARED = [issue(1, "shared-a"), issue(2, "shared-b")]
#: Acknowledged by the demoted primary, never replicated. IIDs 3 and 4.
ORPHANED = [receipt(3, "partition-a"), receipt(4, "partition-b", confidential=True)]
#: Accepted after promotion. The same IIDs 3 and 4 were reissued here.
POST = [receipt(3, "post-a"), receipt(4, "post-b")]


def report(issues):
    return divergence_report(SHARED, ORPHANED, POST, issues)


def promoted_history():
    """The state the agent is handed: shared plus post-promotion, no orphans."""
    return [*SHARED, issue(3, "post-a"), issue(4, "post-b")]


def demoted_history():
    """What restoring the demoted primary's snapshot would produce."""
    return [*SHARED, issue(3, "partition-a"), issue(4, "partition-b", confidential=True)]


def reconciled_history():
    """Both acknowledged sets retained; the orphans carry fresh IIDs."""
    return [
        *promoted_history(),
        issue(5, "partition-a"),
        issue(6, "partition-b", confidential=True),
    ]


def test_content_key_matches_a_receipt_to_the_row_that_stores_it():
    """Receipts say `project`, snapshot rows say `project_id`; both must agree."""
    assert content_key(receipt(3, "partition-a")) == content_key(issue(99, "partition-a"))


def test_the_starting_state_is_graded_as_abandoning_the_orphaned_writes():
    outcome = report(promoted_history())

    assert outcome["orphaned_missing"] == ["partition-a", "partition-b"]
    assert outcome["post_promotion_displaced"] == []
    assert not both_histories_retained(outcome)


def test_failing_back_to_the_demoted_snapshot_loses_the_post_promotion_writes():
    """The naive restore. It recovers the orphans and destroys newer writes."""
    outcome = report(demoted_history())

    assert outcome["orphaned_missing"] == []
    assert outcome["post_promotion_displaced"] == [
        {"project": PROJECT, "iid": 3},
        {"project": PROJECT, "iid": 4},
    ]
    assert not both_histories_retained(outcome)


def test_reconciling_both_histories_passes():
    outcome = report(reconciled_history())

    assert outcome["orphaned_missing"] == []
    assert outcome["orphaned_duplicated"] == []
    assert outcome["orphaned_recovered"] == 2
    assert outcome["post_promotion_displaced"] == []
    assert outcome["shared_history_displaced"] == []
    assert both_histories_retained(outcome)


def test_unrelated_later_issues_do_not_break_recovery():
    """Validation probes and ordinary new work add rows; that is not a failure."""
    outcome = report([*reconciled_history(), issue(7, "probe"), issue(8, "ordinary work")])

    assert both_histories_retained(outcome)


def test_reconciling_twice_is_graded_as_duplication():
    outcome = report([*reconciled_history(), issue(9, "partition-a")])

    assert outcome["orphaned_duplicated"] == ["partition-a"]
    assert not both_histories_retained(outcome)


def test_a_duplicated_post_promotion_write_is_also_rejected():
    outcome = report([*reconciled_history(), issue(9, "post-a")])

    assert outcome["post_promotion_duplicated"] == 1
    assert not both_histories_retained(outcome)


def test_losing_the_shared_history_is_reported_separately():
    outcome = report([row for row in reconciled_history() if row["title"] != "shared-b"])

    assert outcome["shared_history_displaced"] == [{"project": PROJECT, "iid": 2}]
    assert not both_histories_retained(outcome)


def test_an_orphan_reinserted_over_a_post_promotion_identity_fails():
    """Reusing the colliding IID looks like recovery but overwrites live work."""
    outcome = report([*SHARED, issue(3, "partition-a"), issue(4, "post-b"), issue(5, "partition-b", confidential=True)])

    assert outcome["orphaned_missing"] == []
    assert outcome["post_promotion_displaced"] == [{"project": PROJECT, "iid": 3}]
    assert not both_histories_retained(outcome)


@pytest.mark.parametrize(
    ("issues", "reason"),
    [
        (promoted_history(), "orphaned_writes_abandoned"),
        (demoted_history(), "post_promotion_writes_lost"),
        ([*reconciled_history(), issue(9, "partition-a")], "acknowledged_writes_duplicated"),
        ([row for row in reconciled_history() if row["title"] != "shared-a"], "shared_history_changed"),
    ],
)
def test_each_shortfall_gets_its_own_reason_code(issues, reason):
    """`reason` reaches the results CSV, so the two failures must be separable."""
    verdict = GitLabDivergentRecoveryOracle.verdict(report(issues))

    assert verdict["reason"] == reason
    assert verdict["failure_class"] == FailureClass.AGENT_ERROR


def test_a_complete_recovery_produces_no_verdict():
    assert GitLabDivergentRecoveryOracle.verdict(report(reconciled_history())) is None


def test_every_reason_the_oracle_can_return_is_classified():
    """A reason missing from the table would be silently filed as ambiguous."""
    classes = GitLabDivergentRecoveryOracle._failure_classes()
    for reason in (
        "database_schema_missing",
        "post_promotion_writes_lost",
        "orphaned_writes_abandoned",
        "acknowledged_writes_duplicated",
        "shared_history_changed",
        "recovery_evidence_changed",
        "acknowledged_git_data_changed",
    ):
        assert classes[reason] == FailureClass.AGENT_ERROR
    # Inherited from SaaSOracle, and still reachable through super().evaluate().
    assert classes["acknowledged_record_missing"] == FailureClass.AGENT_ERROR
    assert classes["database_membership_changed"] == FailureClass.AGENT_ERROR
