"""Lifecycle validation of pretix_waitinglist_closes_quota in DinD (no agent, no LLM).

Each cycle: fresh deploy -> baseline -> inject -> age -> symptom check (quota closed with
free seats, waiting list unserved, door-station warning) -> mitigation oracle must FAIL ->
the cycle's mitigation steps in order, oracle after each -> the last step must PASS.
A "healthy" cycle runs the same traffic with close-when-sold-out off (the carts lapse with
people waiting, the cron serves them) and expects the oracle to pass and no warning.

    python tests/integration/apps/pretix/validate_waitinglist_closes_quota.py --out results/v.json \
        --cycle healthy:1500 --cycle plain_reopen,reference:1500 --cycle shell_assign:480 \
        --cycle disable_close:480 --cycle raise_capacity:300
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from logger import init_logger  # noqa: E402
from sregym.conductor.problems.apps.pretix import waitinglist_closes_quota as P  # noqa: E402

record = {"problem": "pretix_waitinglist_closes_quota", "cycles": []}
started = time.time()


def log(msg, **data):
    print(f"[{round(time.time() - started)}s] {msg} {json.dumps(data, default=str)[:3000]}", flush=True)


def kubectl(*args, timeout=300):
    return subprocess.run(["kubectl", "-n", "pretix", *args], capture_output=True, text=True, timeout=timeout).stdout


def wait_gone():
    for _ in range(90):
        if not subprocess.run(
            ["kubectl", "get", "ns", "pretix", "--ignore-not-found", "-o", "name"], capture_output=True, text=True
        ).stdout.strip():
            return
        time.sleep(5)


def quota_log(problem):
    q = problem.target["quota"]
    return (
        problem.app.psql(
            "SELECT to_char(datetime, 'HH24:MI:SS.MS'), action_type, coalesce(user_id::text, '-'), "
            f"coalesce(api_token_id::text, '-') FROM pretixbase_logentry WHERE content_type_id = (SELECT id FROM "
            f"django_content_type WHERE model='quota') AND object_id={q} AND action_type LIKE 'pretix.event.quota.%' "
            "ORDER BY id"
        )
        .strip()
        .splitlines()
    )


# ---------------------------------------------------------------- mitigation steps
def step_reference(problem):
    problem.recover_fault()


def step_plain_reopen(problem):
    """Reopen the quota through the API, nothing else."""
    problem.api("PATCH", f"/quotas/{problem.target['quota']}/", {"closed": False})
    time.sleep(3)
    return {"closed_after_3s": problem._closed(problem.target["quota"]), "log": quota_log(problem)[-3:]}


def step_raise_capacity(problem):
    """Make room by raising the quota size by the number of waiting people and reopening."""
    q = problem.api("GET", f"/quotas/{problem.target['quota']}/")
    waiting = len([e for e in problem._entries(problem.target["item"]) if not e["voucher"]])
    problem.api("PATCH", f"/quotas/{q['id']}/", {"size": q["size"] + waiting, "closed": False})


def step_delete_waiting(problem):
    """Remove the waiting list entries and reopen (the seats go back on sale)."""
    for e in problem._entries(problem.target["item"]):
        if not e["voucher"]:
            problem.api("DELETE", f"/waitinglistentries/{e['id']}/")
    problem.api("PATCH", f"/quotas/{problem.target['quota']}/", {"closed": False})


def step_shell_assign(problem):
    """In a Django shell: reopen and run the waiting-list assignment in one step (close-when-sold-out stays on)."""
    return problem.app.pretix_python(SHELL_ASSIGN, {"quota": problem.target["quota"]})


def step_disable_close(problem):
    """Reopen with close-when-sold-out switched off and leave it off; the cron serves the list."""
    problem.api("PATCH", f"/quotas/{problem.target['quota']}/", {"closed": False, "close_when_sold_out": False})


def step_undo_capacity_disable_close(problem):
    """Put the room capacity back, then reopen with close-when-sold-out off (left off)."""
    q = problem.api("GET", f"/quotas/{problem.target['quota']}/")
    problem.api("PATCH", f"/quotas/{q['id']}/", {"size": problem.target["seats"]})
    step_disable_close(problem)


SHELL_ASSIGN = r"""
from django.db import transaction
from django_scopes import scopes_disabled
from pretix.base.models import Quota
from pretix.base.services.waitinglist import assign_automatically
with scopes_disabled():
    q = Quota.objects.get(pk=ARGS["quota"])
    with transaction.atomic():
        q.closed = False
        q.save(update_fields=["closed"])
        q.log_action("pretix.event.quota.opened")
    sent = assign_automatically.apply(args=(q.event_id,)).get()
    print(json.dumps({"sent": sent}))
"""

STEPS = {
    "reference": step_reference,
    "plain_reopen": step_plain_reopen,
    "raise_capacity": step_raise_capacity,
    "delete_waiting": step_delete_waiting,
    "shell_assign": step_shell_assign,
    "disable_close": step_disable_close,
    "undo_capacity_disable_close": step_undo_capacity_disable_close,
}


def run_cycle(steps, aging):
    healthy = steps == ["healthy"]
    problem = P.PretixWaitingListClosesQuota()
    if healthy:
        problem.app.profile["close_when_sold_out"] = False
    app, oracle = problem.app, problem.mitigation_oracle
    cyc = {"steps": steps, "aging_s": aging}
    record["cycles"].append(cyc)
    app.cleanup()
    wait_gone()
    t = time.time()
    app.deploy()
    cyc["deploy_s"] = round(time.time() - t)
    oracle.capture_baseline()
    log("deployed", seconds=cyc["deploy_s"])
    t = time.time()
    try:
        problem.inject_fault()
    except RuntimeError as exc:
        if not healthy:
            raise
        cyc["healthy_inject_note"] = str(exc)
        problem._stop.clear()
    cyc["inject_s"] = round(time.time() - t)
    cyc["observed"] = problem.observed
    cyc["target"] = problem.target
    log("injected", seconds=cyc["inject_s"], observed=problem.observed, target=problem.target)
    problem.handover_time = time.time() + aging
    time.sleep(aging)
    cyc["quota_log"] = quota_log(problem)
    cyc["entries"] = [(e["id"], e["voucher"]) for e in problem._entries(problem.target["item"])]
    lines = kubectl("logs", "deploy/door-scanner", "--tail=3000").splitlines()
    cyc["door_warnings"] = [ln for ln in lines if "waiting for a seat" in ln]
    cyc["buyers"] = dict(problem.buyers.stats)
    log("symptom", quota_log=cyc["quota_log"][-4:], entries=cyc["entries"], warnings=cyc["door_warnings"][-3:])
    cyc["oracle_before"] = oracle.evaluate()
    log("oracle before" if not healthy else "healthy oracle", result=cyc["oracle_before"])
    cyc["after"] = []
    for step in [] if healthy else steps:
        t = time.time()
        out = STEPS[step](problem)
        res = oracle.evaluate()
        cyc["after"].append({"step": step, "seconds": round(time.time() - t), "out": out, "oracle": res})
        log(f"oracle after {step}", out=out, result=res)
    problem._stop.set()
    if problem.buyers:
        problem.buyers.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cycle", action="append", required=True, help="steps,comma,separated:aging_seconds")
    args = ap.parse_args()
    init_logger()
    try:
        for spec in args.cycle:
            steps, aging = spec.split(":")
            run_cycle(steps.split(","), int(aging))
            with open(args.out, "w") as fh:
                json.dump(record, fh, indent=1, default=str)
    finally:
        record["total_s"] = round(time.time() - started)
        with open(args.out, "w") as fh:
            json.dump(record, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
