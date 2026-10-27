from types import SimpleNamespace
from unittest.mock import Mock

from sregym.conductor.problems import kubelet_crash


def test_diagnosis_describes_the_all_worker_injection(monkeypatch):
    app = SimpleNamespace(namespace="astronomy-shop", create_workload=Mock())
    monkeypatch.setattr(kubelet_crash, "AstronomyShop", lambda: app)
    monkeypatch.setattr(kubelet_crash, "KubeCtl", Mock())
    monkeypatch.setattr(kubelet_crash, "RemoteOSFaultInjector", Mock())
    monkeypatch.setattr(kubelet_crash, "LLMAsAJudgeOracle", Mock())

    problem = kubelet_crash.KubeletCrash()

    assert "all worker nodes" in problem.root_cause
    assert "control plane" in problem.root_cause
    assert "whole application" in problem.root_cause
    assert "the affected node" not in problem.root_cause
    assert "component=node/kubelet; namespace=astronomy-shop" in problem.root_cause
    kubelet_crash.LLMAsAJudgeOracle.assert_called_once_with(problem=problem, expected=problem.root_cause)
