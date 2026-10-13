import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.service.apps.app_registry import APPLICATIONS, AppDefinition, AppRegistry, RecoveryChecks
from sregym.service.apps.base import Application
from sregym.service.apps.gitlab.notifications import (
    UNANNOUNCED_AMBIGUOUS_NOTIFICATIONS,
    GitLabNotificationApplication,
    GitLabNotificationUnannouncedAmbiguityApplication,
)


def test_catalog_discovery_does_not_construct_applications(monkeypatch):
    from sregym.service import kubectl

    monkeypatch.setattr(kubectl.KubeCtl, "__init__", Mock(side_effect=AssertionError("cluster access")))
    registry = AppRegistry()
    assert {"gitea", "gitlab", "mattermost", "stripe", "train-ticket", "flight-ticket"} <= set(registry.get_app_ids())
    for definition in APPLICATIONS:
        metadata = registry.get_app_metadata(definition.app_id)
        assert registry.get_definition(metadata["Name"]) is definition
        assert registry.get_app_config_file(definition.app_id).is_file()
        for alias in definition.aliases:
            assert registry.get_definition(alias) is definition


@pytest.mark.parametrize("app_id", ["App", "app_name", "-app", "app-", "app--name", "app/part"])
def test_ids_use_one_canonical_format(app_id):
    with pytest.raises(ValueError, match="Invalid application ID"):
        AppDefinition(app_id, Mock(), Path("unused"))


def test_new_app_registration_needs_no_shared_fault_change(tmp_path, monkeypatch):
    from sregym.conductor.problems import wrong_service_selector as selector

    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"Name": "Example application", "Namespace": "example"}))
    factory = Mock(return_value=SimpleNamespace(namespace="example", frontend_port=8088, create_workload=Mock()))
    definition = AppDefinition("example", factory, metadata)
    registry = AppRegistry((definition,))
    monkeypatch.setattr(selector, "AppRegistry", lambda: registry)
    monkeypatch.setattr(selector, "KubeCtl", Mock())
    monkeypatch.setattr(selector, "LLMAsAJudgeOracle", Mock())
    oracle_factory = Mock()

    problem = selector.WrongServiceSelector(app_name="example", faulty_service="api", oracle_factory=oracle_factory)

    factory.assert_called_once_with()
    oracle_factory.assert_called_once_with(problem=problem)
    problem.app.create_workload.assert_called_once_with()
    assert problem.namespace == "example"
    assert problem.expected_service_port == 8088


def test_explicit_factories_do_not_require_catalog_registration(monkeypatch):
    from sregym.conductor.problems import wrong_service_selector as selector

    monkeypatch.setattr(selector, "AppRegistry", Mock(side_effect=AssertionError("catalog lookup")))
    monkeypatch.setattr(selector, "KubeCtl", Mock())
    monkeypatch.setattr(selector, "LLMAsAJudgeOracle", Mock())
    app = SimpleNamespace(namespace="custom", frontend_port=9999, create_workload=Mock())
    oracle = Mock()
    problem = selector.WrongServiceSelector(app_factory=lambda: app, oracle_factory=oracle)
    assert problem.app is app
    oracle.assert_called_once_with(problem=problem)


@pytest.mark.parametrize(
    "name,tier,checks,port",
    [
        ("astronomy_shop", None, RecoveryChecks.ENDPOINTS, None),
        ("hotel_reservation", None, RecoveryChecks.ENDPOINTS, None),
        ("hotel_reservation", "replicated", RecoveryChecks.MONGODB_RECORDS, None),
        ("social_network", None, RecoveryChecks.ENDPOINTS, 9090),
        ("social_network", "replicated", RecoveryChecks.MONGODB_RECORDS, 9090),
        ("gitea", "single", RecoveryChecks.GIT_REPOSITORIES, None),
        ("gitlab_ce", "replicated", RecoveryChecks.POSTGRES_RECORDS, None),
        ("stripe_marathon", "single", RecoveryChecks.POSTGRES_RECORDS, None),
        ("mattermost", None, RecoveryChecks.POSTGRES_RECORDS, None),
    ],
)
def test_existing_selector_configuration_keeps_its_invariants(name, tier, checks, port):
    definition = AppRegistry().get_definition(name)
    assert definition.checks(tier) is checks
    assert definition.service_port == port


def test_catalog_rejects_ambiguous_names_and_ids(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"Name": "same"}))
    first = AppDefinition("first", Mock(), path)
    with pytest.raises(ValueError, match="Duplicate application ID"):
        AppRegistry((first, first))
    with pytest.raises(ValueError, match="Duplicate application lookup name"):
        AppRegistry((first, AppDefinition("second", Mock(), path)))
    with pytest.raises(ValueError, match="not found in registry"):
        AppRegistry((first,)).get_definition("missing")
    with pytest.raises(ValueError, match="Scaling is not supported"):
        first.create("replicated")


def test_missing_deployment_fails_explicitly():
    app = Application(AppRegistry().get_app_config_file("gitea"))
    with pytest.raises(NotImplementedError, match="does not implement deployment"):
        app.deploy()


def test_notification_preset_preserves_the_existing_combination():
    preset = GitLabNotificationApplication("replicated", preset=UNANNOUNCED_AMBIGUOUS_NOTIFICATIONS)
    compatibility = GitLabNotificationUnannouncedAmbiguityApplication("replicated")
    assert preset.render() == compatibility.render()
    assert preset.get_app_json() == compatibility.get_app_json()
