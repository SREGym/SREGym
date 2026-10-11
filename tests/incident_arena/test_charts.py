"""Render every Incident Arena problem's chart exactly as SREGym installs it."""

import shutil
import subprocess

import pytest
import yaml

from sregym.conductor.problems.incident_arena import INCIDENT_ARENA_PROBLEM_CLASSES

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")

HARNESS_RESOURCES = {
    "agent-egress-proxy",
    "agent-egress-tls-gateway",
    "agent-egress-controller",
    "agent-dns-filter",
    "agent-freezer",
    "main",
    "obs-mcp",
    "prometheus",
    "loki",
    "promtail",
    "grader-broker",
}


def _render(problem, tmp_path):
    app = problem.app
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(yaml.safe_dump(app.deploy_overrides))
    out = subprocess.run(
        [
            "helm",
            "template",
            app.helm_configs["release_name"],
            app.helm_configs["chart_path"],
            "-n",
            app.namespace,
            "-f",
            str(app.values_file),
            "-f",
            str(overrides),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


@pytest.mark.parametrize("problem_cls", INCIDENT_ARENA_PROBLEM_CLASSES, ids=lambda c: c.PROBLEM_ID)
def test_chart_renders_without_harness(offline_cluster, tmp_path, problem_cls):
    problem = problem_cls()
    docs = _render(problem, tmp_path)
    names = {(d["kind"], d["metadata"]["name"]) for d in docs}

    leaked = {name for kind, name in names if name in HARNESS_RESOURCES and kind != "Service"}
    assert not leaked, f"Harbor harness rendered under SREGym: {leaked}"
    assert not any(kind == "NetworkPolicy" for kind, _ in names)
    assert ("Deployment", "ops-toolbox") in names

    workloads = {d["metadata"]["name"]: d for d in docs if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet")}
    loadgen = workloads["loadgen"]
    # SREGym's agent visibility policy hides pods labelled app=load-generator.
    assert loadgen["metadata"]["labels"]["app"] == "load-generator"
    assert loadgen["spec"]["template"]["metadata"]["labels"]["app"] == "load-generator"
    env = {e["name"]: e.get("value") for e in loadgen["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["PROFILE"] == problem.app.deploy_overrides["loadgen"]["profile"]
    # SREGym pins the episode start itself, possibly long after the sidecar boots.
    assert float(env["EPISODE_START_TIMEOUT_S"]) >= 86400

    # The load generator's answer key is the fault-free placeholder.
    grader_key = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "loadgen-grader-key")
    neutral_key = yaml.safe_load(grader_key["data"]["ground-truth.yaml"])
    assert neutral_key["scenario"] == "sregym-healthy-baseline"
    assert set(neutral_key) <= {"scenario", "docker_state"}

    # Every pinned image is a registry reference SREGym nodes can pull.
    for workload in workloads.values():
        for container in workload["spec"]["template"]["spec"]["containers"]:
            assert not container["image"].endswith(":dev"), container["image"]


def test_slack_problem_specific_knobs_render(offline_cluster, tmp_path):
    from sregym.conductor.problems.incident_arena.slack_spine import (
        SlackMaintenanceCollision,
        SlackSendsFailStrictMode,
    )

    docs = _render(SlackSendsFailStrictMode(), tmp_path)
    channel = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "svc-channel")
    env = {e["name"]: e.get("value") for e in channel["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ACL_HOLD_MS"] == "350"
    # The pool fault is injected later; the deployed config is healthy.
    app_yaml = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "app-config")
    assert yaml.safe_load(app_yaml["data"]["app.yaml"])["roles"]["channel"]["db"]["pool_size"] == 20

    docs = _render(SlackMaintenanceCollision(), tmp_path)
    db = next(d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "db")
    containers = {c["name"]: c for c in db["spec"]["template"]["spec"]["containers"]}
    maintenance = next(c for name, c in containers.items() if name != "postgres")
    maintenance_env = {e["name"]: e.get("value") for e in maintenance.get("env", [])}
    assert maintenance_env.get("MAINTENANCE_OFFSET_S") == "55"


def test_frappe_queue_broker_deploys_healthy_flags(offline_cluster, tmp_path):
    from sregym.conductor.problems.incident_arena.frappe import FrappeWritesAndQueueOOM

    docs = _render(FrappeWritesAndQueueOOM(), tmp_path)
    scripts = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "frappe-redis-queue-scripts"
    )
    script = scripts["data"]["start-master.sh"]
    assert 'ARGS+=("64mb")' in script and 'ARGS+=("-blpop")' not in script
    cache = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "frappe-redis-cache-scripts")
    assert 'ARGS+=("allkeys-lru")' in cache["data"]["start-master.sh"]
