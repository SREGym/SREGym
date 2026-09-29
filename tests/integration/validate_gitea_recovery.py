"""Exercise negative controls that distinguish availability from complete recovery.

Run after installing CloudNativePG, separately from any benchmark attempt.
"""

import argparse
import json
import subprocess
from pathlib import Path

from sregym.conductor.oracles.gitea import GiteaOracle
from sregym.conductor.problems.gitea_database_deletion import GiteaDatabaseDeletion


def validate(tier):
    problem = GiteaDatabaseDeletion(tier)
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": "gitea", "incident": "database_deletion", "tier": tier, "passed": False}
    try:
        app.deploy()
        oracle.capture_baseline()
        result["healthy"] = oracle.evaluate()
        assert result["healthy"]["success"], result["healthy"]
        problem.inject_fault()
        result["deleted"] = oracle.evaluate()
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
        result["old_baseline_replication"] = None
        GiteaOracle.verify_replication(oracle, oracle.baseline["token"])
        app.run_client(
            "verify",
            **oracle.baseline,
            fixture=json.loads(
                (Path(__file__).parents[2] / "sregym/service/apps/fixtures/gitea-zoo/import-data.json").read_text()
            ),
        )
        result["old_baseline_replication"] = True
        result["backup_only"] = oracle.evaluate()
        assert result["backup_only"]["reason"] == "acknowledged_data_missing_or_changed", result["backup_only"]
        first = problem.receipts[: len(problem.receipts) // 2]
        app.recovery_client("replay", receipts=first)
        result["partial_replay"] = oracle.evaluate()
        assert not result["partial_replay"]["success"], result["partial_replay"]
        app.recovery_client("replay", receipts=problem.receipts)
        before = app.snapshot()
        result["second_replay"] = app.recovery_client("replay", receipts=problem.receipts)
        assert result["second_replay"]["created"] == 0
        assert app.snapshot() == before
        result["recovered"] = oracle.evaluate()
        assert result["recovered"]["success"], result["recovered"]
        app.command("rollout", "restart", "deployment/gitea")
        app.command("rollout", "status", "deployment/gitea", "--timeout=300s", timeout=320)
        app.wait_database()
        result["restarted"] = oracle.evaluate()
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
