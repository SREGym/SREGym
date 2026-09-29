"""Live SaaS admission: faults, record loss, restarts, switchover and reset.

Run inside DinD after scripts/prepare_saas_prototypes.py. This exercises the
registered problem's application, fault injector, reference repair and oracle.
"""

import argparse
import datetime
import json
import time
import traceback
from pathlib import Path

from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector

APPLICATIONS = {"gitlab_ce": "gitlab-ce", "mattermost": "mattermost", "stripe_marathon": "stripe-marathon"}


def check(outcome, expected, label):
    if outcome.get("success") is not expected:
        raise RuntimeError(f"{label}: {outcome}")
    return outcome


def validate(application, tier):
    start = time.monotonic()
    problem = WrongServiceSelector(app_name=application, faulty_service=APPLICATIONS[application], scale_tier=tier)
    app, oracle = problem.app, problem.mitigation_oracle
    result = {"application": application, "tier": tier, "passed": False, "checks": {}}

    def record(name, value):
        result["checks"][name] = value
        print(f"CHECK {application}/{tier} {name}: {json.dumps(value)}", flush=True)

    try:
        app.deploy()
        app.start_workload()
        oracle.capture_baseline()
        record("healthy", check(oracle.evaluate(), True, "healthy"))
        problem.inject_fault()
        record("fault_detected", check(oracle.evaluate(), False, "selector fault"))
        problem.recover_fault()
        # EndpointSlice reconciliation is asynchronous after changing selectors.
        time.sleep(3)
        record("reference_recovery", check(oracle.evaluate(), True, "reference recovery"))
        token = oracle.baseline["token"]
        if application == "gitlab_ce":
            corrupt = f"UPDATE issues SET title='damaged-{token}' WHERE title='sregym-{token}';"
            repair = f"UPDATE issues SET title='sregym-{token}' WHERE title='damaged-{token}';"
        elif application == "mattermost":
            corrupt = f"UPDATE posts SET message='damaged-{token}' WHERE message='sregym-{token}';"
            repair = f"UPDATE posts SET message='sregym-{token}' WHERE message='damaged-{token}';"
        else:
            customer = oracle.baseline["customer"]
            if not customer.startswith("cus_") or not customer.replace("_", "").isalnum():
                raise ValueError("Unexpected generated customer ID")
            path = "{customers," + customer + ",metadata,probe}"
            corrupt = f"UPDATE marathon_state SET state=jsonb_set(state, '{path}', '\"damaged\"') WHERE id=1;"
            repair = f"UPDATE marathon_state SET state=jsonb_set(state, '{path}', '\"{token}\"') WHERE id=1;"
        app.sql(corrupt)
        record("record_loss_detected", check(oracle.evaluate(), False, "retained record corruption"))
        app.sql(repair)
        record("record_restored", check(oracle.evaluate(), True, "record restored"))
        if tier == "replicated":
            original = app.cluster()["status"]["currentPrimary"]
            target = next(p["metadata"]["name"] for p in app.database_pods() if p["metadata"]["name"] != original)
            app.command(
                "patch",
                "cluster.postgresql.cnpg.io",
                app.cluster_name,
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
            if app.cluster()["status"]["currentPrimary"] != target:
                raise RuntimeError("Switchover did not reach target primary")
            record("switchover", {"from": original, "to": target, **check(oracle.evaluate(), True, "switchover")})
            app.command("delete", "pod", original, "--wait=true", "--timeout=90s")
            app.wait_database(timeout=300)
            record("database_pod_replacement", check(oracle.evaluate(), True, "database replacement"))
        if application == "stripe_marathon":
            app.command("scale", "deployment/stripe-worker", "--replicas=0")
            app.command("wait", "--for=delete", "pod", "-l", "app=stripe-worker", "--timeout=90s")
            pending = app.run_client("probe", token="a" * 32, wait_webhook=False)
            jobs = int(
                app.sql("SELECT count(*) FROM marathon_state, jsonb_object_keys(state->'delivery_jobs') WHERE id=1;")
            )
            if jobs < 1:
                raise RuntimeError("Webhook work was not persisted while the worker was stopped")
            record("worker_outage_detected", check(oracle.evaluate(), False, "worker stopped"))
        for name in (app.slug, *app.auxiliary_deployments):
            app.command("rollout", "restart", f"deployment/{name}")
            if application == "stripe_marathon" and name == "stripe-worker":
                app.command("scale", "deployment/stripe-worker", "--replicas=1")
            app.command(
                "rollout",
                "status",
                f"deployment/{name}",
                f"--timeout={app.startup_timeout}s",
                timeout=app.startup_timeout + 30,
            )
        record("application_restart", check(oracle.evaluate(), True, "application restart"))
        if application == "stripe_marathon":
            record("persisted_backlog_drained", app.run_client("verify", **pending))
        result["images"] = [
            {
                "pod": p["metadata"]["name"],
                "containers": [
                    {"name": c["name"], "image": c["image"], "imageID": c.get("imageID")}
                    for c in p.get("status", {}).get("containerStatuses", [])
                ],
            }
            for p in json.loads(app.command("get", "pods", "-o", "json"))["items"]
        ]
        result["baseline"] = oracle.baseline
        result["volumes"] = oracle.volumes
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["stderr"] = (getattr(exc, "stderr", "") or "")[-4000:]
        traceback.print_exc()
    finally:
        try:
            app.cleanup()
            remaining = json.loads(app.command("get", "pv", "-o", "json"))["items"]
            if any(p["spec"].get("claimRef", {}).get("namespace") == app.namespace for p in remaining):
                raise RuntimeError("Application volumes were not reclaimed")
            record("cleanup", {"passed": True})
        except Exception as exc:
            result.update(passed=False, cleanup_error=str(exc))
    result["duration_seconds"] = time.monotonic() - start
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", choices=APPLICATIONS, required=True)
    parser.add_argument("--tier", choices=("single", "replicated"), default="replicated")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outcome = validate(args.application, args.tier)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outcome, indent=2) + "\n")
    print(json.dumps(outcome, indent=2))
    raise SystemExit(int(not outcome["passed"]))
