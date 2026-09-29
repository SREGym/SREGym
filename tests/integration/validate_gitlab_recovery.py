"""Exercise negative controls that distinguish availability from complete recovery.

Run after installing CloudNativePG, separately from any benchmark attempt.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from sregym.conductor.problems.gitlab_database_deletion import GitLabDatabaseDeletion


def validate(tier):
    problem = GitLabDatabaseDeletion(tier)
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": "gitlab-ce", "incident": "database_deletion", "tier": tier, "passed": False}
    started = time.time()

    def grade(stage):
        outcome = oracle.evaluate()
        print(
            json.dumps({"stage": stage, "elapsed_seconds": round(time.time() - started, 2), "grade": outcome}),
            flush=True,
        )
        return outcome

    try:
        app.deploy()
        pods = json.loads(app.command("get", "pods", "-o", "json"))["items"]
        result["images"] = {
            c["image"]: c.get("imageID") for p in pods for c in p.get("status", {}).get("containerStatuses", [])
        }

        oracle.capture_baseline()
        result["healthy"] = grade("healthy")
        assert result["healthy"]["success"], result["healthy"]
        problem.inject_fault()
        result["deleted"] = grade("deleted")
        assert result["deleted"]["reason"] == "database_schema_missing", result["deleted"]
        try:
            app.restore_archive("/recovery/backups/latest.dump")
        except subprocess.CalledProcessError:
            result["truncated_backup_rejected"] = True
        else:
            raise AssertionError("The truncated backup unexpectedly passed restore validation")
        app.restore_archive()
        # The base oracle demonstrates that HTTP and old baseline data are back.
        # Its write probe would consume a post-snapshot issue number, so use a
        # read-only workflow here before testing the remaining recovery tail.
        app.run_client("verify", **oracle.baseline)
        result["old_baseline_available"] = True
        result["backup_only"] = grade("backup_only")
        assert result["backup_only"]["reason"] == "acknowledged_data_missing_or_changed", result["backup_only"]
        first = problem.receipts[: len(problem.receipts) // 2]
        app.recovery_client("replay", receipts=first)
        result["partial_replay"] = grade("partial_replay")
        assert not result["partial_replay"]["success"], result["partial_replay"]
        app.recovery_client("replay", receipts=problem.receipts)
        before = app.snapshot()
        result["second_replay"] = app.recovery_client("replay", receipts=problem.receipts)
        assert result["second_replay"]["created"] == 0
        assert app.snapshot() == before
        problem.recover_fault()
        result["recovered"] = grade("recovered")
        assert result["recovered"]["success"], result["recovered"]
        app.command("rollout", "restart", "deployment/gitlab-ce")
        app.command("rollout", "status", "deployment/gitlab-ce", "--timeout=1500s", timeout=1530)
        app.wait_database()
        result["restarted"] = grade("restarted")
        assert result["restarted"]["success"], result["restarted"]
        result["passed"] = True
    except Exception as exc:
        result["error"] = str(exc)
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
    parser.add_argument("--tier", choices=("single", "replicated"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcome = validate(args.tier)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps(outcome, indent=2))
    raise SystemExit(int(not outcome["passed"]))
