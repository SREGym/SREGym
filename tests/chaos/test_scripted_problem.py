from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.chaos import protected, slo, telemetry
from sregym.chaos.artifact import discover_faults
from sregym.conductor.problems.scripted_problem import ScriptedProblem, scripted_problems

BUILTIN_PROBLEM_IDS = {
    "consul_stale_snapshot_restore_hotel_reservation",
    "frontend_endpoint_transport_mismatch_hotel_reservation",
    "frontend_form_field_limit_social_network",
    "mongodb_snapshot_write_lock_social_network",
    "post_cache_field_pair_serialization_social_network",
    "profile_address_encryption_mismatch_hotel_reservation",
    "profile_cache_shell_literal_serialization_hotel_reservation",
    "profile_cache_unresolved_object_references_hotel_reservation",
    "profile_storage_eviction_threshold_units_hotel_reservation",
    "rate_service_registration_drift_hotel_reservation",
    "timeline_cache_acl_key_restriction_social_network",
    "timeline_cache_client_limit_social_network",
    "timeline_cache_output_buffer_units_social_network",
    "timeline_cache_replica_write_quorum_social_network",
    "timeline_checkpoint_member_pollution_social_network",
    "timeline_snapshot_path_misconfiguration_social_network",
}


def test_builtin_fault_names_and_layout(monkeypatch):
    monkeypatch.delenv("SREGYM_FAULT_DIRS", raising=False)
    faults = discover_faults()
    assert set(faults) == BUILTIN_PROBLEM_IDS
    for problem_id, artifact in faults.items():
        assert artifact.problems() == []
        assert problem_id == f"{artifact.path.name}_{artifact.app}"
        assert artifact.path.parent.name == "faults"
        assert artifact.path.parent.parent.name == artifact.app


def test_every_builtin_factory_selects_its_application(monkeypatch):
    monkeypatch.delenv("SREGYM_FAULT_DIRS", raising=False)
    monkeypatch.setattr("sregym.service.apps.hotel_reservation.KubeCtl", Mock)
    monkeypatch.setattr("sregym.service.apps.social_network.KubeCtl", Mock)
    monkeypatch.setattr("sregym.generators.workload.wrk2.config.load_kube_config", lambda: None)
    monkeypatch.setattr("sregym.generators.workload.wrk2.client.CoreV1Api", Mock)
    monkeypatch.setattr("sregym.generators.workload.wrk2.client.BatchV1Api", Mock)
    factories = scripted_problems()
    assert set(factories) == BUILTIN_PROBLEM_IDS
    for problem_id, factory in factories.items():
        problem = factory()
        assert isinstance(problem, ScriptedProblem)
        assert problem.problem_id == problem_id
        assert problem.namespace == problem.artifact.app.replace("_", "-")
        assert problem.diagnosis_oracle is None
        assert problem.mitigation_oracle.problem is problem
        assert not problem.fault_injected


def test_registry_keeps_existing_and_scripted_ids(monkeypatch):
    from sregym.conductor.problems.registry import ProblemRegistry

    monkeypatch.delenv("SREGYM_FAULT_DIRS", raising=False)
    monkeypatch.setattr("sregym.conductor.problems.registry.KubeCtl", Mock)
    entries = ProblemRegistry().PROBLEM_REGISTRY
    assert BUILTIN_PROBLEM_IDS.issubset(entries)
    assert "misconfig_app_hotel_res" in entries
    assert "service_wrong_pod_selection_hotel_reservation" in entries
    assert not any(problem_id.startswith("gen_") for problem_id in entries)


def test_scripted_ids_cannot_replace_existing_registry_entries(monkeypatch):
    from sregym.conductor.problems.registry import ProblemRegistry

    monkeypatch.setattr(
        "sregym.conductor.problems.registry.scripted_problems",
        lambda: {"misconfig_app_hotel_res": Mock()},
    )
    with pytest.raises(ValueError, match="Scripted problem IDs already registered: misconfig_app_hotel_res"):
        ProblemRegistry()


@pytest.fixture
def problem(monkeypatch):
    healthy = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10) for i in range(5)]
    broken = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10, non2xx=1500) for i in range(5)]
    policy = slo.SLO(target_rps=100)
    oracle = SimpleNamespace(
        slo=policy, baseline_verdict=slo.judge(policy, healthy), measure=Mock(return_value=slo.judge(policy, broken))
    )
    p = ScriptedProblem.__new__(ScriptedProblem)
    p.problem_id = "scripted_example"
    p.namespace = "app"
    p.fault_injected = False
    p.mitigation_oracle = oracle
    p.alerts = SimpleNamespace(capture_baseline=lambda: None, new=lambda: [])
    p.workspace = None
    p.artifact = SimpleNamespace(
        workspace=Mock(
            return_value=SimpleNamespace(run=Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr="")))
        )
    )
    p._write_report = Mock()
    monkeypatch.setattr(protected, "snapshot", lambda _: {"Job/app/workload": "original"})
    monkeypatch.setattr(protected, "leak_scan", lambda _: {})
    monkeypatch.setattr(protected, "workload_running", lambda _: True)
    monkeypatch.setattr(telemetry.Signals, "capture_baseline", lambda _: None)
    monkeypatch.setattr(telemetry.Signals, "changes", lambda _: {"log_surges": {}, "cpu_surges": {}})
    monkeypatch.setenv("CHAOS_PERSISTENCE_S", "0")
    return p


def test_real_request_failures_are_observable_without_new_alerts(problem):
    problem.inject_fault()
    assert problem.fault_injected
    assert problem.gates["impact_pass"]
    assert problem.gates["observability_pass"]


def test_no_effect_injection_is_setup_failure(problem):
    problem.mitigation_oracle.measure.return_value = problem.mitigation_oracle.baseline_verdict
    with pytest.raises(RuntimeError, match="impact_pass"):
        problem.inject_fault()
    assert not problem.fault_injected
    problem._write_report.assert_called_once()
    assert problem.workspace is not None  # Partial injection still needs recovery.


def test_missing_measurement_cannot_establish_impact(problem):
    rounds = [slo.RoundStats(time=i, requests=3000, duration_s=30) for i in range(5)]
    problem.mitigation_oracle.measure.return_value = slo.judge(problem.mitigation_oracle.slo, rounds)
    with pytest.raises(RuntimeError, match="impact_pass"):
        problem.inject_fault()


def test_explicit_outage_is_impact_even_without_completed_request_latencies(problem):
    rounds = [slo.RoundStats(time=i, requests=0, duration_s=30, connect_errors=3000) for i in range(5)]
    problem.mitigation_oracle.measure.return_value = slo.judge(problem.mitigation_oracle.slo, rounds)
    problem.inject_fault()
    assert problem.gates["impact_pass"]
    assert problem.gates["observability_pass"]


def test_stopped_workload_is_setup_failure(problem, monkeypatch):
    monkeypatch.setattr(protected, "workload_running", lambda _: False)
    with pytest.raises(RuntimeError, match="integrity_pass"):
        problem.inject_fault()


def test_modified_measurement_resources_are_setup_failure(problem, monkeypatch):
    snapshots = iter([{"Job/app/workload": "original"}, {"Job/app/workload": "changed"}])
    monkeypatch.setattr(protected, "snapshot", lambda _: next(snapshots))
    with pytest.raises(RuntimeError, match="integrity_pass"):
        problem.inject_fault()


def test_protected_resource_api_failure_is_not_an_empty_snapshot(monkeypatch):
    import subprocess

    monkeypatch.setattr(
        protected.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=1, stdout="", stderr="API unavailable"),
    )
    with pytest.raises(subprocess.CalledProcessError):
        protected.snapshot("app")
