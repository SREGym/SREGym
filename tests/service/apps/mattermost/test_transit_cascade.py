import json
import re
from pathlib import Path

import pytest
import yaml

from sregym.service.agent_visibility_policy import HIDDEN_NAMESPACES
from sregym.service.apps.mattermost import transit_cascade
from sregym.service.apps.mattermost.transit_cascade import PROVIDER_NAMESPACE, MattermostTransitApplication

OBSERVER = Path(transit_cascade.__file__).parents[3] / "observer"


@pytest.fixture
def app():
    return MattermostTransitApplication()


# ---- hiding the provider side ------------------------------------------


def test_provider_network_is_hidden_from_the_agent():
    assert PROVIDER_NAMESPACE in HIDDEN_NAMESPACES


def collection_drops():
    """Namespace drop rules applied where metrics and logs are collected."""
    prometheus = yaml.safe_load((OBSERVER / "prometheus" / "prometheus" / "values.yaml").read_text())
    jobs = {job["job_name"]: job for job in prometheus["serverFiles"]["prometheus.yml"]["scrape_configs"]}
    drops = {
        name: [
            rule["regex"]
            for rule in jobs[name].get("metric_relabel_configs", [])
            if rule.get("action") == "drop" and rule.get("source_labels") == ["namespace"]
        ]
        for name in ("kube-state-metrics", "kubernetes-cadvisor")
    }
    promtail = yaml.safe_load((OBSERVER / "loki" / "promtail-values.yaml").read_text())
    drops["promtail"] = [
        rule["regex"]
        for rule in promtail["config"]["snippets"]["extraRelabelConfigs"]
        if rule.get("action") == "drop" and rule.get("source_labels") == ["__meta_kubernetes_namespace"]
    ]
    return drops


def test_provider_network_telemetry_is_never_collected():
    """Aggregations drop the namespace label, so filtering at query time is not enough."""
    for collector, regexes in collection_drops().items():
        assert any(re.fullmatch(regex, PROVIDER_NAMESPACE) for regex in regexes), collector
        assert not any(re.fullmatch(regex, "mattermost") for regex in regexes), collector


def test_cluster_component_logs_about_provider_objects_are_never_collected():
    """Controllers and the CNI log provider pods as namespace/name in their own namespaces."""
    promtail = yaml.safe_load((OBSERVER / "loki" / "promtail-values.yaml").read_text())
    stages = promtail["config"]["snippets"]["pipelineStages"]
    assert stages[0] == {"cri": {}}  # the chart's default parsing stays first
    drops = [stage["drop"]["expression"] for stage in stages if "drop" in stage]

    def dropped(line):
        return any(re.search(expression, line) for expression in drops)

    assert dropped('"Finished syncing" kind="ReplicaSet" key="provider-network/customers-748f99d5bf"')
    assert dropped('WorkloadId:"provider-network/transit-7489949bc-ltm47"')
    # The gateway's own errors name its upstream address, which the agent can already read.
    assert not dropped("upstream error: <urlopen error timed out> http://transit.provider-network.svc:8065/")


# ---- what the agent is told --------------------------------------------


def test_description_states_topology_not_diagnosis(app):
    description = app.get_app_json()["Desc"]
    assert "managed network" in description and "not under your administration" in description
    for leak in ("packet", "saturat", "open files", "ulimit", "surge", "degraded", "throttl", "README"):
        assert leak not in description, leak


def test_description_states_the_capacity_requirement_not_how_to_meet_it(app):
    description = app.get_app_json()["Desc"]
    assert f"at least {app.capacity_floor} `chat-gateway` instances in service" in description
    for hint in (r"\bmin\b", "minimum", "scale-in", "scale in", "policy", "scaler.json", "floor"):
        assert not re.search(hint, description), hint


def without_images(value):
    """Image references name their registry, which is not something the agent is told."""
    if isinstance(value, dict):
        return {k: without_images(v) for k, v in value.items() if k != "image"}
    if isinstance(value, list):
        return [without_images(v) for v in value]
    return value


def test_nothing_rendered_names_the_benchmark(app):
    rendered = json.dumps(without_images([*app.render(), *app.provider_documents()])).lower()
    for marker in ("sregym", "chaos", "inject", r"\bfault", "benchmark"):
        assert not re.search(marker, rendered), marker


# ---- problem -----------------------------------------------------------


def test_problem_grades_three_causes_separately_and_ships_no_cause_to_the_agent():
    from sregym.conductor.oracles.apps.mattermost.transit_recovery import MattermostTransitOracle
    from sregym.conductor.problems.apps.mattermost.transit_saturation import MattermostTransitSaturationProblem

    problem = MattermostTransitSaturationProblem()
    assert problem.diagnosis_oracle.expected == problem.root_causes
    components = [cause.split(";")[0].removeprefix("[fault_spec] component=") for cause in problem.root_causes]
    assert components[1:] == ["capacity-scaler", "provisioner"]
    assert isinstance(problem.mitigation_oracle, MattermostTransitOracle)
    description = problem.app.get_app_json()["Desc"]
    for cause in ("CPU", "open-files", "ulimit", "Too many open files", "drops"):
        assert cause not in description, cause


def test_only_the_screened_tier_is_registered():
    """Tier 2 is the tier whose clean screen showed agent errors; tiers 1 and 1b saturated."""
    from sregym.conductor.problems.registry import ProblemRegistry

    registry = ProblemRegistry().PROBLEM_REGISTRY
    assert [problem_id for problem_id in registry if "transit" in problem_id] == ["mattermost_transit_saturation_tier2"]
    problem = registry["mattermost_transit_saturation_tier2"]()
    assert problem.scale_tier == "2"
    assert len(problem.root_causes) == 4
