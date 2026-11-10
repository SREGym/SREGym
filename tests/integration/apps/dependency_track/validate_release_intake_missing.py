"""Lifecycle check for dependency_track_release_intake_missing, without an agent.

deploy (conductor path) -> healthy oracle passes -> inject (the natural events
plus the driver) -> handover checks (no driver leftovers) -> propagation wait ->
symptom through ordinary observability -> mitigation oracle fails -> blind fixes
(re-analysis, re-running the release pipeline without removing the dependency,
api-server and violation-tracker restarts) leave it failing -> operator
mitigation by hand (``--variant``: ``reference`` deletes the moment component and
re-runs the release pipeline, ``reupload`` uploads the release SBOM without the
dependency and then with it) -> oracle passes -> recover_fault() -> oracle still
passes.

``--healthy`` deploys, runs the workload for ``--healthy-minutes`` and checks the
oracle without injecting.

    python tests/integration/apps/dependency_track/validate_release_intake_missing.py results/out.json [--variant NAME]
    python tests/integration/apps/dependency_track/validate_release_intake_missing.py results/healthy.json --healthy
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from logger import init_logger  # noqa: E402
from sregym.conductor.conductor import Conductor, ConductorConfig  # noqa: E402
from sregym.conductor.problems.apps.dependency_track.release_intake_missing import (  # noqa: E402
    DependencyTrackReleaseIntakeMissing,
)

PROBLEM = "dependency_track_release_intake_missing"
VARIANTS = ("reference", "reupload")

NS = "dependency-track"


def kubectl(*args, input_text=None, timeout=300):
    return subprocess.run(
        ["kubectl", "-n", NS, *args], input=input_text, capture_output=True, text=True, timeout=timeout
    ).stdout


def logs(deployment, since=None, grep=None, tail=None):
    args = ["logs", f"deploy/{deployment}"]
    if since:
        args.append(f"--since={since}")
    lines = kubectl(*args, timeout=180).splitlines()
    if grep:
        lines = [line for line in lines if grep in line]
    return lines[-tail:] if tail else lines


def api_warnings(since="40m"):
    lines = logs("dt-api-server", since=since)
    out = {}
    for line in lines:
        for level in (" WARN ", " ERROR "):
            if level in line:
                key = line.split(level, 1)[1].split("[", 1)[1].split("]", 1)[0] if "[" in line else "?"
                msg = line.split("] ", 1)[1][:90] if "] " in line else line[:90]
                out[f"{level.strip()} {key}: {msg}"] = out.get(f"{level.strip()} {key}: {msg}", 0) + 1
    return out


def driver_leftovers(problem):
    """Anything the conductor's driver could have left behind: extra DB sessions, exec processes, pods."""
    sessions = problem.app.psql(
        "SELECT application_name, state, count(*) FROM pg_stat_activity WHERE datname = 'dtrack' "
        "AND pid <> pg_backend_pid() GROUP BY 1, 2 ORDER BY 1, 2"
    )
    locks = problem.app.psql(
        "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
        "WHERE a.datname = 'dtrack' AND (l.locktype = 'tuple' OR NOT l.granted)"
    )
    pods = kubectl("get", "pods", "-o", "custom-columns=NAME:.metadata.name,PHASE:.status.phase", "--no-headers")
    postgres = [line for line in logs("postgres") if "WARNING" in line or "ERROR" in line or "FATAL" in line]
    return {
        "sessions": sessions.strip().splitlines(),
        "waiting_or_tuple_locks": locks.strip(),
        "pods": pods.split(),
        "postgres_warnings": postgres[-10:],
    }


class Recorder:
    def __init__(self, path, problem_id):
        self.path = path
        self.started = time.time()
        self.record = {"problem": problem_id, "stages": {}}

    def __call__(self, name, **data):
        data["t"] = round(time.time() - self.started, 1)
        self.record["stages"][name] = data
        print(f"\n===== {name}: {json.dumps(data, default=str)[:4000]}", flush=True)
        Path(self.path).write_text(json.dumps(self.record, indent=1, default=str))


def poll(oracle, want, timeout):
    deadline = time.time() + timeout
    while True:
        result = oracle.evaluate()
        if result.get("success") is want or time.time() > deadline:
            return result
        time.sleep(30)


class ReleaseIntake:
    """Problem-specific observation, blind fixes and operator mitigations."""

    def __init__(self, problem):
        self.problem = problem
        self.app = problem.app

    def trigger_time(self):
        return self.problem.incident["release_upload_at"]

    def _state(self):
        incident = self.problem.incident or {}
        _, name, version = self.problem.RELEASE
        out = {
            "tracker_warnings": logs("violation-tracker", grep="without", tail=4),
            "tracker_audit": self.app.ops("tracker_audit()", timeout=60),
            "ci_release_lines": [
                line[:320]
                for line in logs("ci-pipelines", since="120m")
                if f'"pipeline": "{name}"' in line and version in line
            ][-14:],
            "api_server_warn_error": api_warnings(since="90m"),
            "postgres_errors": [line[:300] for line in logs("postgres") if "ERROR" in line or "DETAIL" in line][-8:],
        }
        if incident.get("project_uuid"):
            uuid = incident["project_uuid"]
            out["release"] = {
                "findings": self.app.admin_call("findings", uuid),
                "intake": self.app.ops(f"intake({uuid!r})", timeout=120),
                "violations": len(self.app.admin_call("violations", uuid, timeout=300)),
                "tickets": len(self.app.ops(f"tickets({uuid!r})", timeout=120)),
                "runs": [(r["workflow_name"], r["status"]) for r in self.problem.runs(uuid)],
            }
        return out

    def observe(self):
        return self._state()

    def healthy_observe(self):
        return self._state()

    def blind_fixes(self):
        uuid = self.problem.incident["project_uuid"]
        repository, _, version = self.problem.RELEASE
        self.app.admin_call("analyze", uuid, as_admin=True)
        time.sleep(120)
        self.problem.push(repository, version)
        self.problem.wait_ci_idle()
        kubectl("rollout", "restart", "deployment/dt-api-server")
        kubectl("rollout", "status", "deployment/dt-api-server", "--timeout=900s", timeout=930)
        self.app.wait_api_ready()
        kubectl("rollout", "restart", "deployment/violation-tracker")
        kubectl("rollout", "status", "deployment/violation-tracker", "--timeout=300s", timeout=330)
        time.sleep(240)
        return [
            "re-analysis of the release",
            "release pipeline re-run (same SBOM, dependency already present)",
            "api-server restart",
            "violation-tracker restart",
        ]

    def mitigate(self, variant):
        uuid = self.problem.incident["project_uuid"]
        repository, name, version = self.problem.RELEASE
        self.problem.wait_ci_idle()
        if variant == "reference":
            removed = self.problem.remove_dependency(uuid)
            self.problem.push(repository, version)
            return {"removed_components": removed, "release_pipeline_rerun": f"{repository}@{version}"}
        if variant == "reupload":
            password = self.app.admin_password or self.app._secret_value("dt-admin-credentials", "password")
            bom = json.loads(self.app.ops(f"export_bom({password!r}, {uuid!r})", timeout=300))
            without = dict(bom, components=[c for c in bom["components"] if c.get("purl") != self.problem.DEPENDENCY])
            tokens = []
            for document in (without, bom):
                text = json.dumps(document)
                token = self.app.ops(f"upload_bom({password!r}, {name!r}, {version!r}, {text!r})", timeout=300)["token"]
                tokens.append(token)
                deadline = time.time() + 600
                while time.time() < deadline:
                    time.sleep(5)
                    if not self.app.ops(f"token_status({password!r}, {token!r})").get("processing"):
                        break
            return {"uploads": tokens, "without": len(without["components"]), "with": len(bom["components"])}
        raise ValueError(variant)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--healthy", action="store_true", help="deploy, run the workload, never inject")
    parser.add_argument("--healthy-minutes", type=int, default=30)
    parser.add_argument("--variant", default="reference", choices=VARIANTS)
    parser.add_argument("--propagation", type=int, default=None, help="override propagation_duration_s")
    args = parser.parse_args()
    stage = Recorder(args.output, PROBLEM)

    init_logger()
    conductor = Conductor(config=ConductorConfig(deploy_loki=False))
    conductor.problem_id = PROBLEM
    problem = DependencyTrackReleaseIntakeMissing()
    conductor.problem, conductor.app = problem, problem.app
    oracle = problem.mitigation_oracle
    hooks = ReleaseIntake(problem)
    try:
        t = time.time()
        conductor.dependency_check(["kubectl", "helm", "docker"])
        conductor.fix_kubernetes()
        conductor.undeploy_app()
        conductor.deploy_app()
        oracle.capture_baseline()
        stage("deploy", seconds=round(time.time() - t), profile=problem.app.profile)
        stage("healthy_oracle", result=oracle.evaluate())
        stage("description", text=problem.app.description)

        if args.healthy:
            time.sleep(args.healthy_minutes * 60)
            stage("healthy_after_workload", result=oracle.evaluate(), **hooks.healthy_observe())
            return

        t = time.time()
        problem.inject_fault()
        stage(
            "inject",
            seconds=round(time.time() - t),
            attempts=problem.attempts,
            incident=problem.incident,
            events=problem.events,
        )
        stage("handover_footprint", **driver_leftovers(problem))
        stage("expected_root_cause", text=problem.root_cause)
        propagation = problem.propagation_duration_s if args.propagation is None else args.propagation
        trigger_to_handover = time.time() - hooks.trigger_time() + propagation
        stage("propagation_wait", seconds=propagation, trigger_to_handover_s=round(trigger_to_handover))
        time.sleep(propagation)
        stage("symptom", **hooks.observe())
        stage("oracle_after_inject", result=oracle.evaluate())

        fixes = hooks.blind_fixes()
        stage("oracle_after_blind_fixes", fixes=fixes, result=oracle.evaluate())

        stage("operator_mitigation", variant=args.variant, **hooks.mitigate(args.variant))
        stage("oracle_after_mitigation", result=poll(oracle, True, 1200))

        t = time.time()
        problem.recover_fault()
        stage("recover", seconds=round(time.time() - t))
        stage("oracle_after_recover", result=oracle.evaluate())
    finally:
        stage("done")


if __name__ == "__main__":
    main()
