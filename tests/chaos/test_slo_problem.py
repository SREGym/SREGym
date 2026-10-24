import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.chaos import protected, slo, telemetry
from sregym.chaos.artifact import discover_faults
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.slo_problem import SLOProblem, slo_problems

BUILTIN_PROBLEM_IDS = {
    "consul_stale_snapshot_restore_hotel_reservation",
    "frontend_endpointslice_wrong_backend_hotel_reservation",
    "frontend_form_field_limit_social_network",
    "mongodb_snapshot_write_lock_social_network",
    "post_cache_record_format_mismatch_social_network",
    "profile_address_encryption_mismatch_hotel_reservation",
    "profile_cache_invalid_json_hotel_reservation",
    "profile_cache_unresolved_json_references_hotel_reservation",
    "profile_mongodb_eviction_unit_mismatch_hotel_reservation",
    "rate_service_wrong_consul_address_hotel_reservation",
    "timeline_checkpoint_ids_in_feed_social_network",
    "timeline_redis_acl_key_restriction_social_network",
    "timeline_redis_client_limit_too_low_social_network",
    "timeline_redis_output_buffer_unit_mismatch_social_network",
    "timeline_redis_replica_write_requirement_social_network",
    "timeline_redis_snapshot_wrong_directory_social_network",
}


def test_builtin_fault_names_and_layout(monkeypatch):
    monkeypatch.delenv("SREGYM_FAULT_DIRS", raising=False)
    faults = discover_faults()
    assert set(faults) == BUILTIN_PROBLEM_IDS
    for problem_id, artifact in faults.items():
        assert artifact.problems() == []
        assert problem_id == f"{artifact.path.name}_{artifact.app}"
        assert artifact.path.parent.name == artifact.app
        assert artifact.path.parent.parent.name == "apps"
        assert artifact.diagnosis is not None


@pytest.fixture
def builtin_problems(monkeypatch):
    monkeypatch.delenv("SREGYM_FAULT_DIRS", raising=False)
    monkeypatch.setattr("sregym.service.apps.hotel_reservation.KubeCtl", Mock)
    monkeypatch.setattr("sregym.service.apps.social_network.KubeCtl", Mock)
    monkeypatch.setattr("sregym.generators.workload.wrk2.config.load_kube_config", lambda: None)
    monkeypatch.setattr("sregym.generators.workload.wrk2.client.CoreV1Api", Mock)
    monkeypatch.setattr("sregym.generators.workload.wrk2.client.BatchV1Api", Mock)
    monkeypatch.setattr(
        "sregym.conductor.oracles.llm_as_a_judge.judge.get_llm_backend_for_judge",
        Mock(side_effect=AssertionError("Problem construction must not initialize a judge backend")),
    )
    return {problem_id: factory() for problem_id, factory in slo_problems().items()}


def test_every_builtin_factory_selects_its_application_and_diagnosis(builtin_problems):
    assert set(builtin_problems) == BUILTIN_PROBLEM_IDS
    for problem_id, problem in builtin_problems.items():
        assert isinstance(problem, SLOProblem)
        assert problem.problem_id == problem_id
        assert problem.namespace == problem.artifact.app.replace("_", "-")
        assert isinstance(problem.diagnosis_oracle, LLMAsAJudgeOracle)
        assert problem.diagnosis_oracle.expected == problem.root_cause
        assert problem.diagnosis_oracle.requires_llm_judge
        assert f"component={problem.artifact.diagnosis['component']}" in problem.root_cause
        assert f"namespace={problem.namespace}" in problem.root_cause
        assert problem.root_cause.endswith(problem.artifact.diagnosis["description"])
        assert problem.mitigation_oracle.problem is problem
        assert not problem.fault_injected


def test_external_bundle_without_diagnosis_remains_mitigation_only(builtin_problems):
    from sregym.chaos.artifact import FaultArtifact

    original = next(iter(builtin_problems.values()))
    metadata = {key: value for key, value in original.artifact.meta.items() if key != "diagnosis"}
    problem = SLOProblem(FaultArtifact(original.artifact.path, metadata))
    assert problem.diagnosis_oracle is None
    assert problem.root_cause == metadata["root_cause_class"]


@pytest.mark.parametrize(
    "stages,expected,preflight_calls",
    [
        (None, ["diagnosis", "mitigation"], 1),
        (("diagnosis",), ["diagnosis"], 1),
        (("mitigation",), ["mitigation"], 0),
    ],
)
def test_builtin_stage_selection_and_judge_preflight(builtin_problems, stages, expected, preflight_calls):
    for problem_id, problem in builtin_problems.items():
        conductor = Conductor.__new__(Conductor)
        conductor.config = ConductorConfig(stages=stages)
        conductor.logger = logging.getLogger("test.slo_stages")
        conductor.problem_id = problem_id
        conductor.problem = problem
        conductor.tasklist = None
        conductor._judge_preflight = Mock()
        conductor._judge_preflight_done = False
        conductor.get_problem_stages()
        conductor._build_stage_sequence()
        conductor._preflight_active_oracles()
        conductor._preflight_active_oracles()
        assert [stage["name"] for stage in conductor.stage_sequence] == expected
        assert conductor._judge_preflight.call_count == preflight_calls


def test_diagnosis_ground_truth_is_not_in_agent_application_info(builtin_problems, monkeypatch):
    from sregym.conductor import conductor_api

    for problem in builtin_problems.values():
        monkeypatch.setattr(
            conductor_api,
            "_conductor",
            SimpleNamespace(app=problem.app, stage_sequence=[{"name": "diagnosis"}, {"name": "mitigation"}]),
        )
        info = asyncio.run(conductor_api.get_app())
        assert info["stages"] == ["diagnosis", "mitigation"]
        assert "root_cause" not in info and "diagnosis" not in info
        assert problem.artifact.diagnosis["description"] not in info["descriptions"]


def test_registry_keeps_existing_and_slo_ids(monkeypatch):
    from sregym.conductor.problems.registry import ProblemRegistry

    monkeypatch.delenv("SREGYM_FAULT_DIRS", raising=False)
    monkeypatch.setattr("sregym.conductor.problems.registry.KubeCtl", Mock)
    entries = ProblemRegistry().PROBLEM_REGISTRY
    assert BUILTIN_PROBLEM_IDS.issubset(entries)
    assert "nightly_rebalance_oom_hotel_reservation" in entries
    assert "cronjob_sidecar_blocks_completion_hotel_reservation" in entries
    assert not any(problem_id.startswith("gen_") for problem_id in entries)


def test_slo_ids_cannot_replace_existing_registry_entries(monkeypatch):
    from sregym.conductor.problems.registry import ProblemRegistry

    monkeypatch.setattr(
        "sregym.conductor.problems.registry.slo_problems",
        lambda: {"nightly_rebalance_oom_hotel_reservation": Mock()},
    )
    with pytest.raises(ValueError, match="SLO problem IDs already registered: nightly_rebalance_oom_hotel_reservation"):
        ProblemRegistry()


@pytest.fixture
def problem(monkeypatch):
    healthy = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10) for i in range(5)]
    broken = [slo.RoundStats(time=i, requests=3000, duration_s=30, p99_ms=10, non2xx=1500) for i in range(5)]
    policy = slo.SLO(target_rps=100)
    oracle = SimpleNamespace(
        slo=policy, baseline_verdict=slo.judge(policy, healthy), measure=Mock(return_value=slo.judge(policy, broken))
    )
    p = SLOProblem.__new__(SLOProblem)
    p.problem_id = "slo_example"
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
