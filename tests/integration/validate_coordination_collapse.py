"""Prove each of the three levers actually bites on a live cluster.

The negative controls are the point of this family:

- restarting the coordinator achieves nothing, because the fault is its data;
- polling the gated endpoint to check readiness never becomes ready;
- rushing admission while caches are cold re-collapses the cluster and the lost
  requests never come back;
- destroying members to force progress fails even when service returns.

Run after installing CloudNativePG, separately from any benchmark attempt.
"""

import argparse
import json
import time
from pathlib import Path

from sregym.conductor.problems.coordination_collapse import CoordinationCollapse
from sregym.service.apps.incident_runtime import coordination_store


def validate(tier):
    problem = CoordinationCollapse(tier)
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": "coordination", "incident": "coordination_collapse", "tier": tier, "passed": False}
    started = time.time()
    stage = "start"

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
        result["recovery_floor_seconds"] = app.recovery_floor_seconds

        stage = "healthy_baseline"
        oracle.capture_baseline()
        # The cluster must be serving before the incident, or the grader cannot
        # show the collapse caused anything -- and the campaign gate, which
        # requires a passing oracle before injection, would reject the problem.
        result["healthy"] = grade("healthy")
        assert result["healthy"]["success"], result["healthy"]

        stage = "inject"
        problem.inject_fault()
        result["healthy_before"] = problem.observed["healthy_before"]
        result["collapse"] = problem.observed["collapse"]
        assert result["collapse"]["aggregated_metrics_blind"], result["collapse"]
        assert result["collapse"]["status_endpoint_stale"], result["collapse"]

        result["collapsed"] = grade("collapsed")
        assert result["collapsed"]["reason"] == "coordination_leader_unstable", result["collapsed"]

        # Negative control: the tool lies. /status claims health while the
        # cluster serves nothing, and the truth endpoint disagrees.
        stage = "stale_tool"
        reported = app.coordinator_request("/status")
        truth = app.truth()
        assert reported["healthy"] and reported["stale"], reported
        assert reported["watch_subscriptions"] < truth["watch_subscriptions"], (reported, truth)
        result["stale_status_vs_truth"] = {
            "status_reports": reported["watch_subscriptions"],
            "actual": truth["watch_subscriptions"],
        }

        # Negative control: restarting the service cannot help, because the
        # degradation is its persisted data.
        stage = "restart_is_useless"
        app.command("rollout", "restart", "deployment/coordinator")
        app.command("rollout", "status", "deployment/coordinator", "--timeout=300s", timeout=330)
        app.wait_for_coordinator()
        after = app.truth()
        assert not after["leader_healthy"], after
        assert after["watch_subscriptions"] == truth["watch_subscriptions"], after
        result["restart_changed_nothing"] = True

        # Negative control: impatience. Probing the gated endpoint restarts the
        # window it waits on, so a loop never finishes.
        stage = "impatience_costs"
        app.operate("shed", watch_subscriptions=coordination_store.WATCH_BUDGET)
        stability, warming, step = app.settings
        probes = []
        for _ in range(3):
            time.sleep(stability / 3)
            probes.append(app.operate("compact").get("error", "compacted"))
        result["compaction_probes"] = probes
        assert all("restarted the stability window" in p for p in probes), probes
        assert not app.truth()["compacted"], app.truth()

        # Waiting without touching it does work.
        stage = "patience_works"
        time.sleep(stability + 10)
        assert app.operate("compact").get("compacted") is True
        assert app.operate("rebuild-scheduler").get("scheduler_state_fresh") is True
        result["sequence_unlocked"] = True

        # Negative control: rushing admission while cold re-collapses it, and
        # the lost requests are permanent.
        stage = "rushed_admission"
        before_dropped = app.truth()["dropped_requests"]
        app.operate("admit", fraction=1.0)
        time.sleep(20)
        rushed = app.truth()
        assert rushed["regressions"] >= 1, rushed
        assert rushed["dropped_requests"] > before_dropped, rushed
        assert not rushed["scheduler_state_fresh"], rushed
        result["rushing_regressed"] = {
            "regressions": rushed["regressions"],
            "dropped_requests": rushed["dropped_requests"],
            "last_regression_reason": rushed["last_regression_reason"],
        }
        result["after_rush"] = grade("after_rush")
        assert not result["after_rush"]["success"], result["after_rush"]

        # The reference recovery: the full sequence, never rushing a gate.
        stage = "reference_recovery"
        recovery_started = time.time()
        app.reference_recovery()
        result["reference_recovery_seconds"] = round(time.time() - recovery_started, 1)
        assert result["reference_recovery_seconds"] >= app.recovery_floor_seconds * 0.9, result[
            "reference_recovery_seconds"
        ]
        result["recovered"] = grade("recovered")
        assert result["recovered"]["success"], result["recovered"]
        # Loss from the deliberate rush above must still be inside the budget,
        # or the budget is too tight for one recoverable mistake.
        assert result["recovered"]["dropped_requests"] <= result["recovered"]["dropped_request_budget"], result[
            "recovered"
        ]

        stage = "durability"
        # The grade above already held capacity for the durability window; a
        # second grade confirms it is steady rather than oscillating.
        result["still_recovered"] = grade("still_recovered")
        assert result["still_recovered"]["success"], result["still_recovered"]

        # Negative control: destroying a member fails even with service back.
        stage = "irreversible_damage"
        app.operate("force-reset", member="coordinator-1")
        result["after_force_reset"] = grade("after_force_reset")
        assert result["after_force_reset"]["reason"] in (
            "cluster_redundancy_destroyed",
            "quorum_permanently_lost",
            "coordination_leader_unstable",
            "coordination_not_serving",
        ), result["after_force_reset"]
        result["force_reset_is_graded"] = True

        stage = "ledger"
        events = app.ledger()
        kinds = sorted({e["event"] for e in events})
        result["ledger_events"] = kinds
        for expected in ("compaction_too_early", "regression", "member_destroyed"):
            assert expected in kinds, kinds

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
    parser.add_argument("--tier", choices=("single", "replicated"), default="single")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcome = validate(args.tier)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps(outcome, indent=2))
    raise SystemExit(int(not outcome["passed"]))
