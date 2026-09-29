"""Validate Gitea's retained data across PostgreSQL switchover and pod restarts.

Install scripts/install_cnpg.py first. Run separately from agent attempts.
"""

import argparse
import datetime
import json
from pathlib import Path

from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector


def validate():
    problem = WrongServiceSelector(app_name="gitea", faulty_service="gitea", scale_tier="replicated")
    app = problem.app
    oracle = problem.mitigation_oracle
    result = {"application": "gitea", "tier": "replicated", "passed": False}
    try:
        app.deploy()
        oracle.capture_baseline()
        healthy = oracle.evaluate()
        assert healthy["success"], healthy
        original = app.cluster()["status"]["currentPrimary"]
        target = next(p["metadata"]["name"] for p in app.database_pods() if p["metadata"]["name"] != original)
        # The official cnpg promote command requests a switchover through this status field.
        app.command(
            "patch",
            "cluster.postgresql.cnpg.io",
            "gitea-db",
            "--subresource=status",
            "--type=merge",
            "-p",
            json.dumps(
                {
                    "status": {
                        "targetPrimary": target,
                        "targetPrimaryTimestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                        "phase": "Switchover in progress",
                        "phaseReason": f"Switching over to {target}",
                    }
                }
            ),
        )
        app.wait_database(timeout=300)
        assert app.cluster()["status"]["currentPrimary"] == target
        assert app.sql("SELECT NOT pg_is_in_recovery();", pod=target) == "t"
        recovered = oracle.evaluate()
        assert recovered["success"], recovered
        result.update(original_primary=original, new_primary=target, switchover_passed=True)
        app.command("delete", "pod", original, "--wait=true", "--timeout=90s")
        app.command("rollout", "restart", "deployment/gitea")
        app.command("rollout", "status", "deployment/gitea", "--timeout=600s", timeout=620)
        app.wait_database(timeout=300)
        restarted = oracle.evaluate()
        assert restarted["success"], restarted
        result.update(passed=True, postgres_replacement_passed=True, gitea_restart_passed=True, volumes_preserved=True)
    except Exception as exc:
        result["error"] = str(exc)
        if detail := getattr(exc, "stderr", None):
            result["stderr"] = detail[-4000:]
    finally:
        try:
            app.cleanup()
        except Exception as exc:
            result.update(passed=False, cleanup_error=str(exc))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(int(not result["passed"]))
