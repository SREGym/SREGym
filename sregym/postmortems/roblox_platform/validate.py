"""Destructive-to-the-episode integration checks; use a dedicated healthy run."""

import argparse
import json
import time
import uuid

from .runner import Run


def validate(run):
    results = {}

    def grade(label, duration=15):
        value = run.grade(duration)
        results[label] = value
        (run.root / "integration-validation.json").write_text(json.dumps(results, indent=2))
        print(label, json.dumps(value), flush=True)
        return value

    assert grade("healthy")["passed"]
    rid = "retry-" + uuid.uuid4().hex
    before = int(run.sql(0, "SELECT coins FROM players WHERE id=0"))
    assert run.workflow(0, rid)["ok"]
    assert run.workflow(0, rid)["ok"]
    after = int(run.sql(0, "SELECT coins FROM players WHERE id=0"))
    # Run this validator without a concurrent traffic process to make this
    # accounting assertion independent of unrelated purchases by player zero.
    assert before - after == 1
    results["idempotent_retry"] = True
    original = run.nomad("job/analytics")
    stopped = json.loads(json.dumps(original))
    stopped["TaskGroups"][0]["Count"] = 0
    try:
        run.nomad("jobs", "POST", {"Job": stopped})
        run.wait(
            lambda: (
                not any(a["JobID"] == "analytics" and a["ClientStatus"] == "running" for a in run.nomad("allocations"))
            ),
            "consumer shutdown",
            attempts=30,
        )
        value = grade("consumer_stopped")
        assert value["valid"] and value["checks"]["workflows"]
        assert not value["passed"] and not value["checks"]["recovery_tail_processed"]
    finally:
        run.nomad("jobs", "POST", {"Job": original})
    run.wait(
        lambda: len(run.consul("health/service/analytics?passing=true")) == original["TaskGroups"][0]["Count"],
        "consumer restore",
    )
    assert grade("backlog_recovered", 20)["passed"]
    try:
        run.sql(0, "UPDATE players SET coins=coins+1 WHERE id=0")
        value = grade("balance_corruption", 10)
        assert value["valid"] and not value["passed"] and not value["checks"]["balances_conserved"]
    finally:
        run.sql(0, "UPDATE players SET coins=coins-1 WHERE id=0")
    assert grade("final_recovered")["passed"]
    results["finished_at"] = time.time()
    (run.root / "integration-validation.json").write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    selected = Run(args.run)
    if (selected.root / "traffic.pid").exists():
        parser.error("stop the background traffic process before integration validation")
    validate(selected)
