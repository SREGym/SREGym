"""Prove that neither single restore recovers the regional-failover divergence.

The negative controls are the point of this family. Failing back to the demoted
primary's snapshot and leaving the orphaned writes alone must each fail, for
their own reason, before reference reconciliation is allowed to pass.

Run after installing CloudNativePG, separately from any benchmark attempt.
"""

import argparse
import json
import time
from pathlib import Path

from sregym.conductor.problems.gitlab_regional_failover import GitLabRegionalFailover
from sregym.service.apps.gitlab_failover import EAST_SNAPSHOT, PARTITION_JOURNAL, SHARED_SNAPSHOT


def validate(tier):
    problem = GitLabRegionalFailover(tier)
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": "gitlab-ce", "incident": "regional_failover", "tier": tier, "passed": False}
    started = time.time()
    stage = "start"

    def records(receipts):
        fields = ("project", "title", "description", "confidential")
        return [{k: r[k] for k in fields} for r in receipts]

    def rebuild_promoted_history():
        """Return to exactly what the agent is handed after the failover.

        Restoring the shared snapshot resets GitLab's internal IID allocation,
        so re-creating the same records in the same order reissues the same
        public identities -- which is what makes the collision reproducible.
        """
        app.restore_archive(SHARED_SNAPSHOT)
        app.wait_for_api()
        app.recovery_client("create", records=records(problem.accepted_after_promotion))

    def grade(name):
        outcome = oracle.evaluate()
        print(
            json.dumps({"stage": name, "elapsed_seconds": round(time.time() - started, 2), "grade": outcome}),
            flush=True,
        )
        return outcome

    try:
        stage = "deploy"
        app.deploy()
        pods = json.loads(app.command("get", "pods", "-o", "json"))["items"]
        result["images"] = {
            c["image"]: c.get("imageID") for p in pods for c in p.get("status", {}).get("containerStatuses", [])
        }

        stage = "healthy"
        oracle.capture_baseline()
        result["healthy"] = grade("healthy")
        assert result["healthy"]["success"], result["healthy"]

        stage = "inject"
        problem.inject_fault()
        result["fixture"] = {
            "orphaned_writes": len(problem.orphaned),
            "post_promotion_writes": len(problem.accepted_after_promotion),
            "colliding_identities": sorted(
                {(r["project"], r["iid"]) for r in problem.orphaned}
                & {(r["project"], r["iid"]) for r in problem.accepted_after_promotion}
            ),
        }
        # The divergence is only interesting if the two histories actually
        # collided on public identities; a disjoint tail is a weaker task.
        assert result["fixture"]["colliding_identities"], result["fixture"]

        # The journal must describe the orphans, or the incident is unsolvable.
        journal = json.loads(app.archive_command("cat", PARTITION_JOURNAL))
        assert [r["title"] for r in journal] == [r["title"] for r in problem.orphaned], journal
        result["journal_entries"] = len(journal)

        result["after_failover"] = grade("after_failover")
        assert result["after_failover"]["reason"] == "orphaned_writes_abandoned", result["after_failover"]

        # Negative control: failing back recovers the orphans and destroys the
        # writes accepted after promotion. It must not be graded as recovery.
        stage = "fail_back"
        app.restore_archive(EAST_SNAPSHOT)
        result["fail_back"] = grade("fail_back")
        assert result["fail_back"]["reason"] == "post_promotion_writes_lost", result["fail_back"]

        # Return to the state the agent is actually handed, then reconcile.
        stage = "restore_promoted_history"
        rebuild_promoted_history()
        result["promoted_history_restored"] = grade("promoted_history_restored")
        assert result["promoted_history_restored"]["reason"] == "orphaned_writes_abandoned", result[
            "promoted_history_restored"
        ]

        # Negative control: half a reconciliation is not a recovery.
        stage = "partial_reconcile"
        half = problem.orphaned[: len(problem.orphaned) // 2]
        app.reconcile_orphans(half)
        result["partial_reconcile"] = grade("partial_reconcile")
        assert result["partial_reconcile"]["reason"] == "orphaned_writes_abandoned", result["partial_reconcile"]

        # Reconciliation completes the half-done history rather than doubling it.
        stage = "reconcile"
        result["mapping"] = app.reconcile_orphans(problem.orphaned)
        assert len(result["mapping"]) == len(problem.orphaned) - len(half), result["mapping"]
        result["reconciled"] = grade("reconciled")
        assert result["reconciled"]["success"], result["reconciled"]
        assert result["reconciled"]["acknowledged_data_loss"] == 0, result["reconciled"]

        # Repeating reference reconciliation must be a no-op, so cleanup after a
        # partially successful attempt cannot itself create duplicates.
        stage = "repeat_reconcile"
        before = app.snapshot()
        assert app.reconcile_orphans(problem.orphaned) == []
        assert app.snapshot() == before
        result["repeat_reconcile_is_noop"] = True

        # Negative control: a genuine duplicate must be detected, not tolerated.
        stage = "duplicate_reconcile"
        app.recovery_client("create", records=records(problem.orphaned[:1]))
        result["duplicate_reconcile"] = grade("duplicate_reconcile")
        assert result["duplicate_reconcile"]["reason"] == "acknowledged_writes_duplicated", result[
            "duplicate_reconcile"
        ]

        # Rebuild a passing history so the restart and evidence checks are run
        # against a recovered system, as an agent would leave it.
        stage = "recover"
        rebuild_promoted_history()
        app.reconcile_orphans(problem.orphaned)
        result["recovered"] = grade("recovered")
        assert result["recovered"]["success"], result["recovered"]

        stage = "evidence"
        for label, path in (("shared", SHARED_SNAPSHOT), ("demoted_primary", EAST_SNAPSHOT)):
            digest = app.archive_command("sha256sum", path).split()[0]
            assert digest == problem.snapshot_sha256[label], (label, digest)
        result["evidence_unchanged"] = True

        stage = "restart"
        app.command("rollout", "restart", "deployment/gitlab-ce")
        app.command("rollout", "status", "deployment/gitlab-ce", "--timeout=1500s", timeout=1530)
        app.wait_database()
        app.wait_for_api()
        result["restarted"] = grade("restarted")
        assert result["restarted"]["success"], result["restarted"]

        stage = "reference_recovery"
        problem.recover_fault()
        result["reference_recovery"] = grade("reference_recovery")
        assert result["reference_recovery"]["success"], result["reference_recovery"]
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{stage}: {exc}"
        if stderr := getattr(exc, "stderr", None):
            result["stderr"] = stderr[-4000:]
    finally:
        try:
            app.cleanup()
        except Exception as exc:
            result.update(passed=False, cleanup_error=str(exc))
    result["elapsed_seconds"] = round(time.time() - started, 2)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("single", "replicated"), default="replicated")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcome = validate(args.tier)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps(outcome, indent=2))
    raise SystemExit(int(not outcome["passed"]))
