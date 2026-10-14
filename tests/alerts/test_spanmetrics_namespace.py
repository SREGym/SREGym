import re
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "service_name, expected_namespace",
    [
        ("ts-contacts-service", "train-ticket"),
        ("ts-voucher-service", "train-ticket"),
        ("ts-ticket-office-service", "train-ticket"),
        ("ts-news-service", "train-ticket"),
        ("ts-avatar-service", "train-ticket"),
        ("checkout", "astronomy-shop"),
        ("product-catalog", "astronomy-shop"),
        ("frontend", "astronomy-shop"),
    ],
)
def test_otlp_spanmetrics_use_the_application_namespace(service_name, expected_namespace):
    values_path = Path(__file__).parents[2] / "sregym/observer/prometheus/prometheus/values.yaml"
    values = yaml.safe_load(values_path.read_text())
    jobs = yaml.safe_load(values["extraScrapeConfigs"])
    job = next(job for job in jobs if job["job_name"] == "otel-spanmetrics-otlp")
    labels = {**job["static_configs"][0]["labels"], "service_name": service_name}
    for rule in job.get("metric_relabel_configs", []):
        value = ";".join(labels.get(label, "") for label in rule.get("source_labels", []))
        if re.fullmatch(rule.get("regex", "(.*)"), value):
            labels[rule["target_label"]] = rule["replacement"]

    assert labels["namespace"] == expected_namespace
