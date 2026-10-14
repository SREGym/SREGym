from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems import environment
from sregym.conductor.problems.environment_ports import PORTED_PROBLEMS


@pytest.mark.parametrize(
    "name,factory",
    [
        (name, factory)
        for name, factory in PORTED_PROBLEMS.items()
        if name.startswith(("service_dns_resolution_failure_", "stale_coredns_config_"))
    ],
)
def test_coredns_metadata_does_not_change_the_application_namespace(offline_port_cluster, monkeypatch, name, factory):
    monkeypatch.setattr(environment, "KubeCtl", Mock)
    monkeypatch.setattr(
        environment,
        "make_app",
        lambda app: SimpleNamespace(namespace=app.replace("_", "-"), create_workload=Mock()),
    )
    monkeypatch.setattr(environment, "LLMAsAJudgeOracle", lambda **kwargs: SimpleNamespace(**kwargs))
    problem = factory()
    assert problem.namespace != "kube-system"
    assert problem.root_cause.startswith("[fault_spec] component=configmap/coredns; namespace=kube-system ||")
    assert problem.diagnosis_oracle.expected == problem.root_cause


@pytest.mark.parametrize("app_name", ["frappe", "saleor", "slack_spine"])
def test_profile_setup_uses_the_existing_application_registry(monkeypatch, app_name):
    app = Mock()
    registry = Mock()
    registry.get_app_instance.return_value = app
    monkeypatch.setattr(environment, "AppRegistry", lambda: registry)
    assert environment.make_app(app_name) is app
    registry.get_app_instance.assert_called_once_with(app_name)
    app.set_load_profile.assert_called_once_with(*environment.LOAD_PROFILES[app_name])
    app.configure.assert_called_once_with(environment.DEPLOY_VALUES.get(app_name, {}))
