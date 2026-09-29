"""Bounded reconstruction: rollout, misleading recovery, containment, and tail."""

import argparse
import json
import time
from pathlib import Path

from sregym.conductor.problems.stripe_feature_config import StripeFeatureConfig


def validate(tier):
    problem = StripeFeatureConfig(tier)
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": app.slug, "incident": "feature_config", "tier": tier, "passed": False}
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
        result["before_permissions"] = json.loads(app.control("safety"))
        assert result["before_permissions"]["source_counts"] == {"feature_old": 120, "feature_new": 120}
        problem.inject_fault()
        result["injected"] = grade("injected")
        assert result["injected"]["reason"] == "recurring_configuration_risk", result["injected"]
        # A good publication alone must not conceal the next bad generation.
        app.control("generate", "feature_old")
        result["healthy_window"] = grade("healthy_window")
        assert result["healthy_window"]["reason"] == "recurring_configuration_risk", result["healthy_window"]
        problem.contain()
        result["contained"] = grade("contained")
        assert result["contained"]["reason"] == "webhook_backlog_incomplete", result["contained"]
        first = list(problem.expected_events)[: len(problem.expected_events) // 2]
        app.requeue(first)
        deadline = time.monotonic() + 90
        while not set(first) <= {r["id"] for r in app.receipts()}:
            assert time.monotonic() < deadline, "Partial replay did not drain"
            time.sleep(1)
        result["partial_replay"] = grade("partial_replay")
        assert result["partial_replay"]["reason"] == "webhook_backlog_incomplete", result["partial_replay"]
        problem.recover_fault()
        result["second_replay"] = app.requeue()
        assert result["second_replay"]["queued"] == 0
        result["recovered"] = grade("recovered")
        assert result["recovered"]["success"], result["recovered"]
        # A corrected query permits ongoing generation, a distinct valid repair.
        app.control_write(
            "query.sql",
            "SELECT name, type FROM system.columns WHERE database='default' AND table='http_requests_features' ORDER BY name\n",
        )
        app.control("settings", {"enabled": True})
        app.command(
            "rollout", "restart", "deployment/stripe-marathon", "deployment/stripe-worker", "deployment/feature-catalog"
        )
        for name in ("stripe-marathon", "stripe-worker", "feature-catalog"):
            app.command("rollout", "status", f"deployment/{name}", "--timeout=180s", timeout=200)
        result["query_fixed_and_restarted"] = grade("query_fixed_and_restarted")
        assert result["query_fixed_and_restarted"]["success"], result["query_fixed_and_restarted"]
        result["generation_history"] = app.command(
            "exec", "deployment/stripe-marathon", "-c", "edge", "--", "cat", "/control/generation.jsonl"
        )
        result["passed"] = True
    except Exception as exc:
        result["error"] = str(exc)
        if stderr := getattr(exc, "stderr", None):
            result["stderr"] = stderr[-6000:]
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
