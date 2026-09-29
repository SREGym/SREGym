"""Exercise a real election and pod replacement in a fresh scaled application.

Run in a prepared DinD/KIND environment, with no other benchmark using the app's
namespace. This is separate from agent evaluation so test actions never enter an
agent's incident history.
"""

import argparse
import contextlib
import json
import secrets
import subprocess
import time
from pathlib import Path

from sregym.service.apps.deathstarbench import ScaledHotelReservation, ScaledSocialNetwork


def wait_for_workflow(app):
    deadline = time.monotonic() + 90
    while True:
        try:
            app.check_workflow()
            return
        except subprocess.SubprocessError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def validate(app):
    name = "mongodb-user" if app.namespace == "hotel-reservation" else "user-mongodb"
    token = secrets.token_hex(16)
    result = {"application": app.namespace, "tier": app.scale_tier, "passed": False}
    try:
        app.deploy()
        volumes = json.loads(app.command("get", "pvc", "-o", "json"))["items"]
        before = {item["metadata"]["name"]: item["metadata"]["uid"] for item in volumes}
        app.mongo_primary(
            name,
            f"""
            var r=db.getSiblingDB('storage-validation').runCommand({{insert:'records',
                documents:[{{_id:{json.dumps(token)}}}], writeConcern:{{w:3,wtimeout:10000}}}});
            assert.commandWorked(r); assert(!r.writeConcernError); assert(!r.writeErrors);
        """,
        )
        primary = app.mongo(name, "print(rs.status().members.filter(function(m){return m.state===1;})[0]._id);")
        member = int(primary.strip().splitlines()[-1])
        result["original_primary"] = member
        # A connection closure is expected during stepdown; the next assertion
        # independently proves an election happened rather than trusting the command.
        with contextlib.suppress(subprocess.CalledProcessError):
            app.mongo(name, "rs.stepDown(60);", member=member)
        other = (member + 1) % 3
        deadline = time.monotonic() + 60
        while True:
            try:
                app.mongo(
                    name,
                    f"""
                    var p=rs.status().members.filter(function(m){{return m.state===1;}});
                    assert.eq(1,p.length); assert.neq({member},p[0]._id);
                """,
                    member=other,
                )
                break
            except subprocess.SubprocessError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        result["new_primary_elected"] = True
        wait_for_workflow(app)
        app.command("delete", "pod", f"{name}-{member}", "--wait=true", "--timeout=90s")
        app.command("rollout", "status", f"statefulset/{name}", "--timeout=180s", timeout=200)
        app.wait_database(name)
        volumes = json.loads(app.command("get", "pvc", "-o", "json"))["items"]
        assert before == {item["metadata"]["name"]: item["metadata"]["uid"] for item in volumes}
        for index in range(3):
            app.mongo(
                name,
                f"rs.slaveOk(); assert(db.getSiblingDB('storage-validation').records.findOne({{_id:{json.dumps(token)}}}));",
                member=index,
            )
        wait_for_workflow(app)
        result.update(passed=True, volumes_preserved=True, data_survived_pod_replacement=True)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", choices=("hotel_reservation", "social_network"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    factory = ScaledHotelReservation if args.application == "hotel_reservation" else ScaledSocialNetwork
    result = validate(factory(tier="replicated"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return int(not result["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
