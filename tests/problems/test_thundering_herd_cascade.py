from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.thundering_herd_cascade import (
    ROOT_CAUSE_DESCRIPTION,
    ThunderingHerdCascadeAstronomyShop,
    _ASSET,
)
from sregym.conductor.problems import thundering_herd_cascade as module


def test_problem_disables_the_unrelated_default_application_workload():
    assert ThunderingHerdCascadeAstronomyShop.run_default_workload is False


def test_problem_module_does_not_leak_hidden_wave_constants():
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "24" not in source
    assert "66VCHSJNUP" not in source


def test_asset_fans_out_ten_list_products_calls():
    source = _ASSET.read_text(encoding="utf-8")
    assert "for _ in range(10):" in source
    assert "product_catalog_stub.ListProducts" in source
    assert "recommendation catalog refetch" in source
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


def test_has_workspace_when_host_source_is_configured():
    from sregym.conductor.problems.base import EditableFile

    problem = ThunderingHerdCascadeAstronomyShop.__new__(ThunderingHerdCascadeAstronomyShop)
    problem.vendored_source_root = None
    problem.editable_files = [
        EditableFile(
            workspace_path="recommendation_server.py",
            pod_path="/app/recommendation_server.py",
            deployment="recommendation",
            configmap_name="recommendation-src-override",
            host_source=str(_ASSET),
        )
    ]
    assert problem.has_workspace() is True
    assert Path(problem.editable_files[0].host_source).name == "thundering_herd_cascade_recommendation.py"


def _problem():
    problem = ThunderingHerdCascadeAstronomyShop.__new__(ThunderingHerdCascadeAstronomyShop)
    problem.namespace = "astronomy-shop"
    problem.fault_injected = False
    problem._injection_attempted = False
    problem.recommendation_deployment = "recommendation"
    problem.source_path = "/app/recommendation_server.py"
    problem.configmap_name = "recommendation-src-override"
    problem.cache_flag = "recommendationCacheFailure"
    problem._replacement_content = "buggy-recommendation"
    problem.app = SimpleNamespace(set_flag=Mock())
    problem.kubectl = SimpleNamespace(
        wait_for_ready=Mock(),
        exec_command_checked=Mock(return_value="        for _ in range(10):"),
    )
    problem.workload = SimpleNamespace(stop=Mock())
    problem.mitigation_oracle = SimpleNamespace(assert_fault_present=Mock())
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
    assert created[0].injected["command"] == [
        "/venv/bin/opentelemetry-instrument",
        "/venv/bin/python",
        "-B",
        "/src-override/recommendation_server.py",
    ]
    assert created[0].injected["extra_env"] == {
        "PYTHONPATH": "/app",
        "PYTHONPYCACHEPREFIX": "/tmp/pycache",
    }
    problem.kubectl.exec_command_checked.assert_called()
    problem.mitigation_oracle.assert_fault_present.assert_called_once_with()
    problem.kubectl.wait_for_ready.assert_called_once()
    assert problem.fault_injected is True


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
    problem.kubectl.exec_command_checked = Mock(side_effect=RuntimeError("grep: not found"))

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
