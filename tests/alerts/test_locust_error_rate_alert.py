from pathlib import Path

import yaml


def _locust_error_rate_rules():
    values_path = Path(__file__).parents[2] / "sregym" / "observer" / "prometheus" / "prometheus" / "values.yaml"
    values = yaml.safe_load(values_path.read_text())
    groups = values["serverFiles"]["alerting_rules.yml"]["groups"]
    return [
        rule
        for group in groups
        for rule in group.get("rules", [])
        if rule.get("alert") == "HighRequestErrorRate" and "locust_requests_num_failures" in rule["expr"]
    ]


def test_locust_error_rate_uses_a_sliding_window():
    # locust_requests_num_* only grow for the lifetime of the load test. A ratio
    # of the raw totals keeps the alert firing long after a fault is fixed, which
    # made astronomy-shop weak oracles fail every attempt (SREGym-Lite runs).
    rules = _locust_error_rate_rules()
    assert {"train-ticket", "astronomy-shop"} == {
        ns for rule in rules for ns in ("train-ticket", "astronomy-shop") if f'namespace="{ns}"' in rule["expr"]
    }
    for rule in rules:
        expression = rule["expr"]
        assert "increase(locust_requests_num_failures" in expression
        assert "increase(locust_requests_num_requests" in expression
        # No bare (unwindowed) selector may remain in the ratio or the volume guard.
        for line in expression.splitlines():
            if "locust_requests_num_" in line:
                assert line.strip().startswith("increase("), line
