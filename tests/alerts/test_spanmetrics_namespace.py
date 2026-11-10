import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def spanmetrics_job(name):
    values = yaml.safe_load((ROOT / "sregym/observer/prometheus/prometheus/values.yaml").read_text())
    return next(job for job in yaml.safe_load(values["extraScrapeConfigs"]) if job["job_name"] == name)


def relabel(job, labels):
    labels = {**job["static_configs"][0].get("labels", {}), **labels}
    for rule in job.get("metric_relabel_configs", []):
        value = ";".join(labels.get(label, "") for label in rule.get("source_labels", []))
        match = re.fullmatch(rule.get("regex", "(.*)"), value)
        if not match:
            continue
        if rule.get("action") == "drop":
            return None
        replacement = rule.get("replacement", "$1")
        labels[rule["target_label"]] = re.sub(
            r"\$(\d+)", lambda m, match=match: match.group(int(m[1])) or "", replacement
        )
    return labels


@pytest.mark.parametrize("job_name", ["otel-spanmetrics", "otel-spanmetrics-otlp"])
@pytest.mark.parametrize(
    "namespace", ["hotel-reservation", "social-network", "train-ticket", "astronomy-shop", "new-app"]
)
def test_namespace_comes_from_trace_metadata(job_name, namespace):
    # The same service name can occur in any application or namespace.
    labels = relabel(spanmetrics_job(job_name), {"service_name": "frontend", "k8s_namespace_name": namespace})
    assert labels["namespace"] == namespace


@pytest.mark.parametrize("job_name", ["otel-spanmetrics", "otel-spanmetrics-otlp"])
@pytest.mark.parametrize(
    "service", ["frontend", "ts-user-service", "user-service", "unknown_service:frontend_service_process"]
)
def test_unknown_namespace_is_not_guessed(job_name, service):
    assert "namespace" not in relabel(spanmetrics_job(job_name), {"service_name": service})


def test_blueprint_normalization_and_workload_filter_are_preserved():
    job = spanmetrics_job("otel-spanmetrics")
    labels = relabel(
        job,
        {
            "service_name": "unknown_service:frontend_service_process",
            "k8s_namespace_name": "blueprint-hotel-reservation",
        },
    )
    assert labels["service_name"] == "frontend_service"
    assert labels["namespace"] == "blueprint-hotel-reservation"
    assert relabel(job, {"service_name": "unknown_service:wlgen_proc"}) is None


def collector_resources():
    return list(yaml.safe_load_all((ROOT / "sregym/observer/otel_collector/otel-collector.yaml").read_text()))


def test_collector_enriches_both_protocols_before_export():
    resources = collector_resources()
    config = yaml.safe_load(next(r for r in resources if r["kind"] == "ConfigMap")["data"]["config.yaml"])
    pipelines = config["service"]["pipelines"]
    assert pipelines["traces"]["processors"] == ["resource/jaeger", "k8sattributes"]
    assert pipelines["traces/otlp"]["processors"] == ["k8sattributes"]
    processor = config["processors"]["k8sattributes"]
    assert processor["extract"]["metadata"] == ["k8s.namespace.name"]
    assert processor["pod_association"] == [
        {"sources": [{"from": "resource_attribute", "name": "k8s.pod.uid"}]},
        {"sources": [{"from": "resource_attribute", "name": "k8s.pod.ip"}]},
        {"sources": [{"from": "connection"}]},
    ]
    assert config["processors"]["resource/jaeger"]["attributes"] == [
        {"key": "k8s.pod.ip", "from_attribute": "ip", "action": "insert"}
    ]
    for connector in config["connectors"].values():
        assert connector["dimensions"] == [{"name": "k8s.namespace.name"}]


def test_collector_metadata_permissions_are_read_only():
    resources = collector_resources()
    deployment = next(r for r in resources if r["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "otel-collector"
    role = next(r for r in resources if r["kind"] == "ClusterRole")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods", "namespaces"], "verbs": ["get", "list", "watch"]}
    ]
    binding = next(r for r in resources if r["kind"] == "ClusterRoleBinding")
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "otel-collector", "namespace": "observe"}]


def test_replica_counters_use_service_and_namespace_identity():
    resources = collector_resources()
    config = yaml.safe_load(next(r for r in resources if r["kind"] == "ConfigMap")["data"]["config.yaml"])
    for connector in config["connectors"].values():
        assert connector["resource_metrics_key_attributes"] == ["service.name", "k8s.namespace.name"]


@pytest.mark.parametrize(
    ("name", "buckets"),
    [
        (
            "spanmetrics",
            ["1ms", "2.5ms", "5ms", "10ms", "25ms", "50ms", "100ms", "250ms", "500ms", "1s", "2.5s", "5s", "10s"],
        ),
        (
            "spanmetrics/otlp",
            ["5ms", "10ms", "25ms", "50ms", "75ms", "100ms", "250ms", "500ms", "750ms", "1s", "1.5s", "2s", "5s"],
        ),
    ],
)
def test_span_histograms_use_explicit_durations(name, buckets):
    configmap = next(resource for resource in collector_resources() if resource["kind"] == "ConfigMap")
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    histogram = config["connectors"][name]["histogram"]
    assert histogram["unit"] == "ms"
    assert histogram["explicit"]["buckets"] == buckets
