import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "docker/kube-proxy/test_rules.py"
SPEC = importlib.util.spec_from_file_location("proxy_rules", PATH)
RULES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RULES)


def fixture_rules(probabilities):
    lines = ["-A KUBE-SVC-TEST ! -s 10.244.0.0/16 -j KUBE-MARK-MASQ"]
    for i in range(5):
        probability = f" --probability {probabilities[i]}" if i < 4 else ""
        lines.append(f'-A KUBE-SVC-TEST --comment "proxy-test/backend:http"{probability} -j KUBE-SEP-{i}')
        lines.append(f"-A KUBE-SEP-{i} -j DNAT --to-destination 10.244.0.{i + 10}:8080")
    return "\n".join(lines)


def test_observed_fixed_point_probabilities_are_accepted():
    checked = RULES.check_rules(fixture_rules([0.01145139989] * 4))
    assert len(checked["service_rules"]) == len(checked["endpoint_rules"]) == 5


def test_healthy_probability_distribution_is_rejected():
    with pytest.raises(AssertionError):
        RULES.check_rules(fixture_rules([0.2, 0.25, 1 / 3, 0.5]))


def test_final_unconditional_endpoint_must_be_reachable():
    rules = fixture_rules([0.01145139989] * 4)
    rules = "\n".join(line for line in rules.splitlines() if not line.endswith("-j KUBE-SEP-4"))
    with pytest.raises(AssertionError):
        RULES.check_rules(rules)


def test_waits_for_initial_rule_sync():
    assert RULES.check_rules("") is None
