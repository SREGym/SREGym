from pathlib import Path

import yaml


def test_cpu_throttling_alert_supports_container_and_pod_cgroup_metrics():
    values_path = Path(__file__).parents[2] / "sregym" / "observer" / "prometheus" / "prometheus" / "values.yaml"
    values = yaml.safe_load(values_path.read_text())
    groups = values["serverFiles"]["alerting_rules.yml"]["groups"]
    rule = next(
        rule for group in groups for rule in group.get("rules", []) if rule.get("alert") == "ContainerCPUThrottling"
    )

    expression = rule["expr"]
    assert 'container!=""' in expression
    assert 'container=""' in expression
    assert "sum by (namespace, pod)" in expression
    assert "{{ $labels.container }}" not in rule["annotations"]["description"]
