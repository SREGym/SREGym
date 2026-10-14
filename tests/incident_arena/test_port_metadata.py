from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.ia_ports import IA_PORT_PROBLEMS
from sregym.conductor.problems.lite_ia import LITE_IA_PROBLEMS, common, k8s


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


@pytest.mark.parametrize("app_name", ["frappe", "saleor", "slack_spine"])
def test_profile_setup_uses_the_existing_application_registry(monkeypatch, app_name):
    app = Mock()
    registry = Mock()
    registry.get_app_instance.return_value = app
    monkeypatch.setattr(common, "AppRegistry", lambda: registry)
    assert common.make_app(app_name) is app
    registry.get_app_instance.assert_called_once_with(app_name)
    app.set_load_profile.assert_called_once_with(*common.LOAD_PROFILES[app_name])
    app.configure.assert_called_once_with(common.DEPLOY_VALUES.get(app_name, {}))
