from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from sregym.conductor.problems import thundering_herd_cascade as module
from sregym.conductor.problems.thundering_herd_cascade import (
    _ASSET,
    ROOT_CAUSE_DESCRIPTION,
    ThunderingHerdCascadeAstronomyShop,
)


def test_problem_disables_the_unrelated_default_application_workload():
    assert ThunderingHerdCascadeAstronomyShop.run_default_workload is False


def test_problem_disables_the_bundled_astronomy_shop_load_generator(monkeypatch):
    configured = {}

    class FakeAstronomyShop:
        namespace = "astronomy-shop"
        frontend_service = "frontend-proxy"
        frontend_port = 8080

        def __init__(self, *, load_generator_enabled):
            configured["load_generator_enabled"] = load_generator_enabled

    monkeypatch.setattr(module, "_RecommendationSourceApp", FakeAstronomyShop)
    monkeypatch.setattr(module, "KubeCtl", Mock)
    monkeypatch.setattr(module, "RecommendationHerdWorkload", Mock)
    monkeypatch.setattr(module, "LLMAsAJudgeOracle", Mock)
    monkeypatch.setattr(module, "ThunderingHerdMitigationOracle", Mock)

    ThunderingHerdCascadeAstronomyShop()

    assert configured["load_generator_enabled"] is False


def test_healthy_deployment_prepares_the_same_source_package_before_injection(monkeypatch):
    deploy = Mock()
    injector = Mock()
    monkeypatch.setattr(module.AstronomyShop, "deploy", deploy)
    monkeypatch.setattr(module, "_wait_for_services", Mock())
    monkeypatch.setattr(module, "ApplicationFaultInjector", Mock(return_value=injector))
    app = module._RecommendationSourceApp.__new__(module._RecommendationSourceApp)
    app.namespace = "astronomy-shop"
    app.kubectl = SimpleNamespace(exec_command_checked=Mock(return_value="healthy image source"))

    app.deploy()

    deploy.assert_called_once()
    args = injector.inject_source_file_override.call_args.kwargs
    assert args["replacement_content"] == "healthy image source"
    assert args["configmap_name"] == ThunderingHerdCascadeAstronomyShop.configmap_name
    assert args["source_path"] == ThunderingHerdCascadeAstronomyShop.source_path
    assert "override" not in args["configmap_name"]


def test_empty_healthy_source_package_fails_before_mounting(monkeypatch):
    injector = Mock()
    monkeypatch.setattr(module, "ApplicationFaultInjector", Mock(return_value=injector))
    app = module._RecommendationSourceApp.__new__(module._RecommendationSourceApp)
    app.namespace = "astronomy-shop"
    app.kubectl = SimpleNamespace(exec_command_checked=Mock(return_value=""))

    with pytest.raises(RuntimeError, match="empty"):
        app.prepare_source_package()

    injector.inject_source_file_override.assert_not_called()


def test_problem_module_does_not_leak_hidden_wave_constants():
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "24" not in source
    assert "66VCHSJNUP" not in source


def test_asset_does_not_explain_the_repair():
    source = _ASSET.read_text(encoding="utf-8")
    assert "for _ in range(10):" not in source
    assert "product_catalog_stub.ListProducts" in source
    assert "recommendation catalog refetch" not in source
    assert "single-flight" not in source
    assert "coalescing" not in source
    assert "ListRecommendations" in source
    assert "GetProduct" not in source
    assert "check_feature_flag" not in source


def test_root_cause_names_duplicated_inflight_work_not_retries():
    text = ROOT_CAUSE_DESCRIPTION.lower()
    assert "listproducts" in text
    assert "single-flight" in text or "coalescing" in text
    assert "retry storm" in text
    assert "x86-64" in ROOT_CAUSE_DESCRIPTION
    assert "24" not in ROOT_CAUSE_DESCRIPTION
    assert "66VCHSJNUP" not in ROOT_CAUSE_DESCRIPTION
    structured = ThunderingHerdCascadeAstronomyShop.build_structured_root_cause(
        component="deployment/recommendation",
        namespace="astronomy-shop",
        description=ROOT_CAUSE_DESCRIPTION,
    )
    assert structured.startswith("[fault_spec] component=deployment/recommendation")


def _problem():
    problem = ThunderingHerdCascadeAstronomyShop.__new__(ThunderingHerdCascadeAstronomyShop)
    problem.namespace = "astronomy-shop"
    problem.fault_injected = False
    problem._injection_attempted = False
    problem.recommendation_deployment = "recommendation"
    problem.source_path = "/app/recommendation_server.py"
    problem.configmap_name = ThunderingHerdCascadeAstronomyShop.configmap_name
    problem.cache_flag = "recommendationCacheFailure"
    problem._replacement_content = "buggy-recommendation"
    problem.app = SimpleNamespace(set_flag=Mock())
    problem.kubectl = SimpleNamespace(
        wait_for_ready=Mock(),
        get_service=Mock(return_value=SimpleNamespace(spec=SimpleNamespace(selector={"app": "recommendation"}))),
        core_v1_api=SimpleNamespace(list_namespaced_pod=Mock(return_value=SimpleNamespace(items=[]))),
        exec_command_checked=Mock(return_value=__import__("hashlib").sha256(b"buggy-recommendation").hexdigest()),
    )
    problem.workload = SimpleNamespace(stop=Mock(), start_background=Mock())
    problem.mitigation_oracle = SimpleNamespace(
        assert_fault_present=Mock(), visible_concurrency=4, seed_product_ids=("OLJCESPC7Z",)
    )
    return problem


def test_repeated_injection_fails_before_mutating_cluster():
    problem = _problem()
    problem._injection_attempted = True

    with pytest.raises(RuntimeError, match="already active"):
        problem.inject_fault()


def test_inject_overlays_recommendation_and_keeps_cache_flag_off(monkeypatch):
    created = []

    class FakeInjector:
        def __init__(self, namespace):
            self.namespace = namespace
            created.append(self)
            self.injected = None
            self.recovered = False

        def inject_source_file_override(self, **kwargs):
            self.injected = kwargs

        def recover_source_file_override(self, **kwargs):
            self.recovered = True

    monkeypatch.setattr(module, "ApplicationFaultInjector", FakeInjector)
    problem = _problem()

    problem.inject_fault()

    assert problem.app.set_flag.call_args.args == ("recommendationCacheFailure", False)
    assert created[0].injected["deployment_name"] == "recommendation"
    assert created[0].injected["source_path"] == "/app/recommendation_server.py"
    assert created[0].injected["replacement_content"] == "buggy-recommendation"
    assert created[0].injected["container_name"] == "recommendation"
    assert "command" not in created[0].injected
    assert "extra_env" not in created[0].injected
    problem.kubectl.exec_command_checked.assert_called()
    problem.mitigation_oracle.assert_fault_present.assert_called_once_with()
    problem.workload.start_background.assert_called_once_with(concurrency=4, product_ids=("OLJCESPC7Z",))
    problem.kubectl.wait_for_ready.assert_called_once()
    assert problem.fault_injected is True


def test_problem_waits_for_previous_pods_to_drain_without_changing_shared_readiness(monkeypatch):
    draining = SimpleNamespace(metadata=SimpleNamespace(name="old-recommendation", deletion_timestamp=object()))
    current = SimpleNamespace(metadata=SimpleNamespace(name="current-recommendation", deletion_timestamp=None))
    kubectl = SimpleNamespace(
        wait_for_ready=Mock(),
        get_service=Mock(return_value=SimpleNamespace(spec=SimpleNamespace(selector={"app": "recommendation"}))),
        core_v1_api=SimpleNamespace(
            list_namespaced_pod=Mock(
                side_effect=[SimpleNamespace(items=[current, draining]), SimpleNamespace(items=[current])]
            )
        ),
    )
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    module._wait_for_services(kubectl, "astronomy-shop", ["recommendation"])

    kubectl.wait_for_ready.assert_called_once_with("astronomy-shop", service_names=["recommendation"])
    assert kubectl.core_v1_api.list_namespaced_pod.call_count == 2
    assert kubectl.core_v1_api.list_namespaced_pod.call_args.kwargs["label_selector"] == "app=recommendation"


def test_problem_reports_a_drain_timeout(monkeypatch):
    draining = SimpleNamespace(metadata=SimpleNamespace(name="old-recommendation", deletion_timestamp=object()))
    kubectl = SimpleNamespace(
        wait_for_ready=Mock(),
        get_service=Mock(return_value=SimpleNamespace(spec=SimpleNamespace(selector={"app": "recommendation"}))),
        core_v1_api=SimpleNamespace(list_namespaced_pod=Mock(return_value=SimpleNamespace(items=[draining]))),
    )
    ticks = iter([0, 0, 0, 181])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="old-recommendation"):
        module._wait_for_services(kubectl, "astronomy-shop", ["recommendation"])


def test_healthy_capacity_overrides_are_scoped_to_this_problem():
    assert module.AstronomyShop.extra_values_files == ()
    assert module._RecommendationSourceApp.extra_values_files == (module._VALUES,)
    values = yaml.safe_load(module._VALUES.read_text())
    catalog = values["components"]["product-catalog"]
    assert catalog["resources"]["requests"]["memory"] == "20Mi"
    assert catalog["resources"]["limits"]["memory"] == "64Mi"
    assert catalog["envOverrides"] == [{"name": "GOMEMLIMIT", "value": "48MiB"}]


def test_inject_removes_overlay_when_fault_verification_fails(monkeypatch):
    recovered = []

    class FakeInjector:
        def __init__(self, namespace):
            pass

        def inject_source_file_override(self, **kwargs):
            return None

        def recover_source_file_override(self, **kwargs):
            recovered.append(kwargs)

    monkeypatch.setattr(module, "ApplicationFaultInjector", FakeInjector)
    problem = _problem()
    problem.mitigation_oracle.assert_fault_present = Mock(side_effect=RuntimeError("not amplifying"))

    with pytest.raises(RuntimeError, match="not amplifying"):
        problem.inject_fault()

    assert recovered and recovered[0]["deployment_name"] == "recommendation"
    problem.workload.stop.assert_called()
    assert problem.fault_injected is False


def test_inject_cleans_up_when_background_traffic_cannot_start(monkeypatch):
    injector = Mock()
    monkeypatch.setattr(module, "ApplicationFaultInjector", Mock(return_value=injector))
    problem = _problem()
    problem.workload.start_background.side_effect = RuntimeError("no tunnel")

    with pytest.raises(RuntimeError, match="no tunnel"):
        problem.inject_fault()

    injector.recover_source_file_override.assert_called_once()
    problem.workload.stop.assert_called_once()
    assert problem.fault_injected is False


def test_inject_removes_overlay_when_mounted_file_is_missing(monkeypatch):
    recovered = []

    class FakeInjector:
        def __init__(self, namespace):
            pass

        def inject_source_file_override(self, **kwargs):
            return None

        def recover_source_file_override(self, **kwargs):
            recovered.append(kwargs)

    monkeypatch.setattr(module, "ApplicationFaultInjector", FakeInjector)
    problem = _problem()
    problem.kubectl.exec_command_checked = Mock(side_effect=RuntimeError("sha256sum: file not found"))

    with pytest.raises(RuntimeError, match="not live"):
        problem.inject_fault()

    assert recovered and recovered[0]["deployment_name"] == "recommendation"
    problem.mitigation_oracle.assert_fault_present.assert_not_called()
    problem.workload.stop.assert_called()
    assert problem.fault_injected is False


def test_recovery_unmounts_the_overlay(monkeypatch):
    recovered = {}

    class FakeInjector:
        def __init__(self, namespace):
            recovered["namespace"] = namespace

        def recover_source_file_override(self, **kwargs):
            recovered["kwargs"] = kwargs

        def inject_source_file_override(self, **kwargs):
            raise AssertionError("recovery must not overlay")

    monkeypatch.setattr(module, "ApplicationFaultInjector", FakeInjector)
    problem = _problem()
    problem.fault_injected = True

    problem.recover_fault()

    assert recovered["kwargs"]["deployment_name"] == "recommendation"
    assert recovered["kwargs"]["container_name"] == "recommendation"
    problem.kubectl.wait_for_ready.assert_called_once()
    problem.workload.stop.assert_called_once()
    assert problem.fault_injected is False


@pytest.mark.parametrize("failure_step", ["remove_overlay", "wait_for_rollout"])
def test_recovery_preserves_fault_state_and_reports_failed_cleanup(monkeypatch, failure_step):
    injector = Mock()
    monkeypatch.setattr(module, "ApplicationFaultInjector", Mock(return_value=injector))
    problem = _problem()
    problem.fault_injected = True
    failed_operation = (
        injector.recover_source_file_override if failure_step == "remove_overlay" else problem.kubectl.wait_for_ready
    )
    failed_operation.side_effect = RuntimeError("recovery did not complete")

    with pytest.raises(RuntimeError, match="recovery did not complete"):
        problem.recover_fault()

    problem.workload.stop.assert_called_once()
    assert problem.fault_injected is True
