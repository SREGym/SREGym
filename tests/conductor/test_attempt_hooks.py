"""Hooks around fault injection, from an agent's registration to the conductor."""

import asyncio
import logging
import sys
import threading
import types

import pytest

from sregym.agent_registry import list_agents
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.hooks import AttemptHookError, FaultContext, load_hooks
from sregym.conductor.oracles.diagnosis_oracle import DiagnosisOracle
from sregym.phases import read_ledger, summarize


class _Problem:
    namespace = "hotel-reservation"
    root_cause = "frontend has the wrong port"
    propagation_duration_s = 0
    mitigation_oracle = None
    diagnosis_oracle = None

    def __init__(self, log):
        self.log = log

    def inject_fault(self):
        self.log.append("inject")


class _Hooks:
    def __init__(self, log, *, fail_before=None):
        self.log = log
        self.fail_before = fail_before
        self.contexts: list[FaultContext] = []

    def before_fault_injection(self, ctx):
        if self.fail_before is not None:
            raise self.fail_before
        self.log.append("before")
        self.contexts.append(ctx)

    def after_fault_injection(self, ctx):
        self.log.append("after")
        self.contexts.append(ctx)


def _bare_conductor(log, hooks=None) -> Conductor:
    """A Conductor without __init__: no cluster services, one diagnosis stage."""
    c = Conductor.__new__(Conductor)
    c.config = ConductorConfig(propagation_override_s=0)
    c.problem = _Problem(log)
    c.problem_id = "demo"
    c.logger = logging.getLogger("test.attempt_hooks")
    c.phases = None
    c.stage_sequence = [{"name": "diagnosis", "evaluation": None}]
    c.fault_injected = False
    c._attempt_closed = False
    c._hooks = hooks
    return c


def test_hooks_fire_before_and_after_injection_then_the_stage_opens():
    log: list[str] = []
    hooks = _Hooks(log)
    c = _bare_conductor(log, hooks)

    asyncio.run(c._advance_to_next_stage(0))

    assert log == ["before", "inject", "after"]
    assert c.waiting_for_agent and c.submission_stage == "diagnosis"
    before, after = hooks.contexts
    assert before == FaultContext(problem_id="demo", namespace="hotel-reservation", fault_injected_at=None)
    assert after.problem_id == "demo" and after.namespace == "hotel-reservation"
    assert isinstance(after.fault_injected_at, float)


def test_the_after_hook_runs_after_the_propagation_wait(monkeypatch):
    log: list[str] = []
    c = _bare_conductor(log, _Hooks(log))
    c.config = ConductorConfig(propagation_override_s=5)

    async def fake_sleep(seconds):
        log.append(f"sleep {seconds}")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(c._advance_to_next_stage(0))

    assert log == ["before", "inject", "sleep 5", "after"]


@pytest.mark.parametrize("checkpoint_duration", [0.0, 30.0])
def test_after_hook_uses_captured_injection_time_before_checkpoint(monkeypatch, checkpoint_duration):
    clock = [1000.0]
    monkeypatch.setattr("sregym.conductor.conductor.time.time", lambda: clock[0])
    log: list[str] = []
    hooks = _Hooks(log)
    c = _bare_conductor(log, hooks)
    observations = []

    def checkpoint():
        clock[0] += checkpoint_duration / 2
        observations.append(clock[0])
        clock[0] += checkpoint_duration / 2

    diagnosis = DiagnosisOracle.__new__(DiagnosisOracle)
    diagnosis.load_diagnosis_checkpoint = checkpoint
    c.problem.diagnosis_oracle = diagnosis

    asyncio.run(c._advance_to_next_stage())

    before, after = hooks.contexts
    assert before.fault_injected_at is None
    assert after.fault_injected_at == c.fault_spec["fault_injected_at"] == 1000.0
    assert observations[0] >= after.fault_injected_at
    assert c.waiting_for_agent and c.submission_stage == "diagnosis"


def test_hooks_run_off_the_event_loop_thread():
    log: list[str] = []
    seen: list[int] = []
    hooks = types.SimpleNamespace(before_fault_injection=lambda ctx: seen.append(threading.get_ident()))
    c = _bare_conductor(log, hooks)

    asyncio.run(c._advance_to_next_stage(0))

    assert seen and seen[0] != threading.get_ident()


def test_no_hooks_behaves_as_before():
    log: list[str] = []
    c = _bare_conductor(log)
    del c._hooks  # a Conductor built elsewhere may predate the attribute

    asyncio.run(c._advance_to_next_stage(0))

    assert log == ["inject"]
    assert c.waiting_for_agent


def test_a_hooks_object_may_define_only_one_side():
    log: list[str] = []
    after_only = types.SimpleNamespace(after_fault_injection=lambda ctx: log.append("after"))
    c = _bare_conductor(log, after_only)

    asyncio.run(c._advance_to_next_stage(0))

    assert log == ["inject", "after"]


@pytest.mark.parametrize("error", [AttemptHookError("graph is empty"), RuntimeError("connection refused")])
def test_a_refusing_before_hook_leaves_the_cluster_unmutated(error):
    log: list[str] = []
    c = _bare_conductor(log, _Hooks(log, fail_before=error))

    with pytest.raises(AttemptHookError, match=str(error)):
        asyncio.run(c._advance_to_next_stage(0))

    assert log == []
    assert c.fault_injected is False
    assert not c.waiting_for_agent


def test_each_hook_is_its_own_phase(tmp_path):
    log: list[str] = []
    c = _bare_conductor(log, _Hooks(log))
    c.bind_phase_ledger(tmp_path / "phases.jsonl")

    asyncio.run(c._advance_to_next_stage(0))

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert set(summary) >= {"hook:before_fault_injection", "inject_fault", "hook:after_fault_injection"}
    assert summary["hook:before_fault_injection"]["end_ts"] <= summary["inject_fault"]["start_ts"]
    assert summary["inject_fault"]["end_ts"] <= summary["hook:after_fault_injection"]["start_ts"]


def test_a_failing_hook_is_recorded_as_an_error_phase(tmp_path):
    log: list[str] = []
    c = _bare_conductor(log, _Hooks(log, fail_before=RuntimeError("cold")))
    c.bind_phase_ledger(tmp_path / "phases.jsonl")

    with pytest.raises(AttemptHookError):
        asyncio.run(c._advance_to_next_stage(0))

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["hook:before_fault_injection"]["outcome"] == "error"
    assert "inject_fault" not in summary


def test_registry_reads_hooks_and_defaults_to_none(tmp_path):
    registry = tmp_path / "agents.yaml"
    registry.write_text(
        "agents:\n"
        "  - name: plain\n"
        "    kickoff_command: python -m x\n"
        "  - name: hooked\n"
        "    kickoff_command: python -m y\n"
        "    hooks: my_adapter.hooks:hooks\n"
    )

    agents = list_agents(registry)

    assert agents["plain"].hooks is None
    assert agents["hooked"].hooks == "my_adapter.hooks:hooks"


def test_load_hooks_imports_module_attribute(monkeypatch):
    module = types.ModuleType("fake_hooks_module")
    module.hooks = object()
    monkeypatch.setitem(sys.modules, "fake_hooks_module", module)

    assert load_hooks("fake_hooks_module:hooks") is module.hooks
    assert load_hooks(None) is None
    assert load_hooks("") is None


@pytest.mark.parametrize("spec", ["no_colon", ":attr", "module:"])
def test_load_hooks_rejects_a_malformed_spec(spec):
    with pytest.raises(ValueError, match="module:attribute"):
        load_hooks(spec)
