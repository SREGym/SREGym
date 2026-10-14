"""Render every Incident Arena problem's chart exactly as SREGym installs it."""

import shutil
import subprocess

import pytest
import yaml

from tests.incident_arena.cases import INCIDENT_ARENA_PROBLEM_CLASSES

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
    if problem.app.app_id == "slack-spine":
        assert loadgen["spec"]["template"]["spec"]["containers"][0]["command"] == [
            "python3",
            "/opt/loadgen-session/entrypoint.py",
        ]
        session = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "loadgen-session")
        assert session["metadata"]["labels"]["app"] == "load-generator"
        for name in ("svc-message", "svc-search"):
            assert workloads[name]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "2"

    # The load generator's answer key is the fault-free placeholder.
    grader_key = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "loadgen-grader-key")
    neutral_key = yaml.safe_load(grader_key["data"]["ground-truth.yaml"])
    assert neutral_key["scenario"] == "sregym-healthy-baseline"
    assert set(neutral_key) <= {"scenario", "docker_state"}

    for workload in workloads.values():
        for container in workload["spec"]["template"]["spec"]["containers"]:
            assert not container["image"].endswith(":dev"), container["image"]


@pytest.mark.parametrize("app_id, name, replicas", [("frappe", "erp-worker-l", 4), ("saleor", "saleor-api", 2)])
def test_healthy_app_capacity(offline_cluster, tmp_path, app_id, name, replicas):
    from sregym.conductor.problems.apps.frappe.desk_and_queue_outage import FrappeDeskAndQueueOutage
    from sregym.conductor.problems.apps.saleor.statement_timeout import SaleorCheckoutStatementTimeoutCanary

    problem = {"frappe": FrappeDeskAndQueueOutage, "saleor": SaleorCheckoutStatementTimeoutCanary}[app_id]()
    docs = _render(problem, tmp_path)
    deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)
    assert deployment["spec"]["replicas"] == replicas
    if app_id == "saleor":
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert "--workers=4" in container["command"]
        assert container["resources"]["limits"]["cpu"] == "4"


def test_frappe_exporter_readiness_does_not_scrape_redis(offline_cluster, tmp_path):
    from sregym.conductor.problems.apps.frappe.desk_and_queue_outage import FrappeDeskAndQueueOutage

    docs = _render(FrappeDeskAndQueueOutage(), tmp_path)
    for name in ("redis-exporter-cache", "redis-exporter-queue"):
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["readinessProbe"]["httpGet"] == {"path": "/health", "port": "http"}
        assert container["env"][0]["name"] == "REDIS_ADDR"
        service = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == name)
        assert service["spec"]["ports"][0]["port"] == 9121


def test_slack_problem_specific_knobs_render(offline_cluster, tmp_path):
    from sregym.conductor.problems.apps.slack_spine.maintenance import SlackMaintenanceCollision
    from sregym.conductor.problems.apps.slack_spine.strict_mode import SlackSendsFailStrictMode

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
    from sregym.conductor.problems.apps.frappe.writes_and_queue_oom import FrappeWritesAndQueueOOM

    docs = _render(FrappeWritesAndQueueOOM(), tmp_path)
    scripts = next(
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "frappe-redis-queue-scripts"
    )
    script = scripts["data"]["start-master.sh"]
    assert 'ARGS+=("64mb")' in script and 'ARGS+=("-blpop")' not in script
    cache = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "frappe-redis-cache-scripts")
    assert 'ARGS+=("allkeys-lru")' in cache["data"]["start-master.sh"]
