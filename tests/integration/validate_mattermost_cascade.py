"""Prove a manual scale-up does not recover the capacity cascade.

The negative controls are the point: scaling the gateway back up while the
CPU-keyed automation still runs must fail, and so must removing the latency while
capacity is held below the floor. Only doing both passes.

Run after installing CloudNativePG, separately from any benchmark attempt.
"""

import argparse
import json
import time
from pathlib import Path

from sregym.conductor.problems.mattermost_capacity_cascade import UPSTREAM_DELAY_MS, MattermostCapacityCascade


def validate(tier):
    problem = MattermostCapacityCascade(tier)
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": "mattermost", "incident": "capacity_cascade", "tier": tier, "passed": False}
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
        result["calibration"] = json.loads(app.read_control("scaler.json"))
        # A policy calibrated against healthy load must leave healthy load alone.
        assert result["calibration"]["scale_in_below"] < result["calibration"]["calibrated_healthy_cpu_percent"], (
            result["calibration"]
        )

        stage = "healthy"
        oracle.capture_baseline()
        result["healthy_latency_p50_ms"] = oracle.baseline_latency_ms
        result["healthy"] = grade("healthy")
        assert result["healthy"]["success"], result["healthy"]
        # The automation must not have been trimming a healthy service.
        assert app.gateway_replicas()[0] >= app.capacity_floor, app.gateway_replicas()

        stage = "inject"
        problem.inject_fault()
        result["cascade"] = problem.observed.get("cascade")
        # The capacity loss must be the automation's own doing, not scripted.
        assert result["cascade"]["replicas_desired"] < app.capacity_floor, result["cascade"]
        assert any("cpu" in str(d.get("decision", "")) for d in result["cascade"]["decisions"]), result["cascade"]
        result["cascaded"] = grade("cascaded")
        assert result["cascaded"]["reason"] in (
            "gateway_capacity_below_floor",
            "capacity_automation_still_shrinking",
        ), result["cascaded"]

        # Negative control: the fix a responder reaches for first. It works for
        # one automation interval and is then undone.
        stage = "manual_scale_up"
        app.command("scale", "deployment/chat-gateway", f"--replicas={app.capacity_floor}")
        result["manual_scale_up"] = grade("manual_scale_up")
        assert result["manual_scale_up"]["reason"] in (
            "capacity_automation_still_shrinking",
            "gateway_capacity_below_floor",
            "gateway_shedding_requests",
            "gateway_latency_unresolved",
        ), result["manual_scale_up"]
        result["reverted_by_automation"] = app.scaler_decisions()[-3:]

        # Negative control: removing the trigger alone. Latency recovers, but the
        # automation has already parked capacity below the floor.
        stage = "latency_only"
        app.set_upstream_delay(0)
        result["latency_only"] = grade("latency_only")
        assert result["latency_only"]["reason"] in (
            "gateway_capacity_below_floor",
            "capacity_automation_still_shrinking",
        ), result["latency_only"]

        # Negative control: stopping the automation alone, with latency restored.
        stage = "automation_only"
        app.set_upstream_delay(UPSTREAM_DELAY_MS)
        app.scaler_policy(enabled=False)
        app.command("scale", "deployment/chat-gateway", f"--replicas={app.capacity_floor}")
        app.command("rollout", "status", "deployment/chat-gateway", "--timeout=300s", timeout=330)
        result["automation_only"] = grade("automation_only")
        assert result["automation_only"]["reason"] in (
            "gateway_shedding_requests",
            "gateway_latency_unresolved",
        ), result["automation_only"]

        # Both halves. Raising the policy floor instead of disabling it, to show
        # the grader accepts any repair that holds capacity.
        stage = "recover"
        app.set_upstream_delay(0)
        app.scaler_policy(min=app.capacity_floor)
        app.command("scale", "deployment/chat-gateway", f"--replicas={app.capacity_floor}")
        app.command("rollout", "status", "deployment/chat-gateway", "--timeout=300s", timeout=330)
        app.wait_for_gateway()
        result["recovered"] = grade("recovered")
        assert result["recovered"]["success"], result["recovered"]
        result["policy_floor_repair_accepted"] = True

        stage = "restart"
        app.command("rollout", "restart", "deployment/chat-gateway")
        app.command("rollout", "status", "deployment/chat-gateway", "--timeout=300s", timeout=330)
        app.wait_for_gateway()
        result["restarted"] = grade("restarted")
        assert result["restarted"]["success"], result["restarted"]

        stage = "control_state_persistence"
        # The control volume outlives the pods, so the delay cannot be cleared by
        # recreating the gateway -- verify the file really is what we left.
        assert app.read_control("upstream_delay_ms").strip() == "0"
        result["control_state_persisted"] = True

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
    parser.add_argument("--tier", choices=("single", "replicated"), default="single")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcome = validate(args.tier)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps(outcome, indent=2))
    raise SystemExit(int(not outcome["passed"]))
