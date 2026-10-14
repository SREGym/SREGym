from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.ia_ports import IA_PORT_PROBLEMS
from sregym.conductor.problems.lite_ia import LITE_IA_PROBLEMS, k8s


@pytest.mark.parametrize(
    "name,factory",
    [
        (name, factory)
        for name, factory in {**IA_PORT_PROBLEMS, **LITE_IA_PROBLEMS}.items()
        if name.startswith(("service_dns_resolution_failure_", "stale_coredns_config_"))
    ],
)
def test_coredns_metadata_does_not_change_the_application_namespace(monkeypatch, name, factory):
    monkeypatch.setattr(k8s, "KubeCtl", Mock)
    monkeypatch.setattr(
        k8s,
        "make_app",
        lambda app: SimpleNamespace(namespace=app.replace("_", "-"), create_workload=Mock()),
    )
    monkeypatch.setattr(k8s, "LLMAsAJudgeOracle", lambda **kwargs: SimpleNamespace(**kwargs))
    problem = factory()
    assert problem.namespace != "kube-system"
    assert problem.root_cause.startswith("[fault_spec] component=configmap/coredns; namespace=kube-system ||")
    assert problem.diagnosis_oracle.expected == problem.root_cause
