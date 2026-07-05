"""Golden tests for the Stratus -> ATIF adapter.

Stratus is SREGym's own agent (no Harbor source). Two fixtures:

* ``stratus_run``     — a REAL run (results/0704_2055), pre-emitter-fix: ToolMessages
                        lack ``tool_call_id`` -> exercises the POSITIONAL fallback and
                        has no token metrics.
* ``stratus_run_ids`` — synthetic, post-emitter-fix: ToolMessages carry
                        ``tool_call_id`` and AIMessages carry ``usage_metadata`` ->
                        exercises ID-based matching + per-step Metrics.
"""

import json
from pathlib import Path

from sregym.traces.adapters import stratus
from sregym.traces.atif import Trajectory

FIXTURES = Path(__file__).parent / "fixtures"
REAL = FIXTURES / "stratus_run"
IDS = FIXTURES / "stratus_run_ids"


def _convert(fixture: Path) -> Trajectory:
    return stratus.to_atif(fixture, sregym_meta={"problem_id": "p", "run": 1})


# --------------------------------------------------------------------------- #
# REAL run (positional fallback, no metrics)
# --------------------------------------------------------------------------- #
def test_real_returns_validated_trajectory():
    t = _convert(REAL)
    assert isinstance(t, Trajectory)
    assert t.schema_version == "ATIF-v1.7"
    assert t.agent.name == "stratus"
    assert t.session_id == "service_port_conflict_hotel_reservation"


def test_real_stage_concatenation():
    t = _convert(REAL)
    stages = t.extra["sregym"]["stages"]
    assert [s["stage"] for s in stages] == [
        "diagnosis",
        "mitigation_attempt_0",
        "mitigation_attempt_1",
    ]
    # Stages are contiguous, non-overlapping, and cover all steps.
    assert stages[0]["first_step"] == 1
    assert stages[-1]["last_step"] == len(t.steps)
    for a, b in zip(stages, stages[1:], strict=False):
        assert b["first_step"] == a["last_step"] + 1


def test_real_step_counts():
    t = _convert(REAL)
    assert len(t.steps) == 28
    sources = [s.source for s in t.steps]
    assert sources.count("system") == 3  # one per stage
    assert sources.count("user") == 3


def test_real_positional_tool_matching_all_linked():
    """Pre-fix run has no tool_call_id; positional fallback still links every result."""
    t = _convert(REAL)
    tool_calls = sum(len(s.tool_calls or []) for s in t.steps)
    observations = sum(len(s.observation.results) for s in t.steps if s.observation)
    assert tool_calls == 30
    assert observations == 30
    # Every observation references a real tool_call_id in its own step (validator
    # also enforces this at construction time).
    for s in t.steps:
        if s.tool_calls and s.observation:
            call_ids = {tc.tool_call_id for tc in s.tool_calls}
            for r in s.observation.results:
                assert r.source_call_id in call_ids


def test_real_no_metrics_pre_fix():
    t = _convert(REAL)
    assert all(s.metrics is None for s in t.steps)
    fm = t.final_metrics
    assert fm.total_prompt_tokens is None
    assert fm.total_steps == 28


def test_real_submitted_and_boundary():
    t = _convert(REAL)
    sregym = t.extra["sregym"]
    assert sregym["submitted"] is True
    # diagnosis stage ends at its last step.
    diag = next(s for s in sregym["stages"] if s["stage"] == "diagnosis")
    assert sregym["diagnosis_submitted_step"] == diag["last_step"]


def test_real_roundtrips():
    t = _convert(REAL)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))


# --------------------------------------------------------------------------- #
# synthetic post-fix run (id-based matching + metrics)
# --------------------------------------------------------------------------- #
def test_ids_shape():
    t = _convert(IDS)
    assert len(t.steps) == 8
    assert [s["stage"] for s in t.extra["sregym"]["stages"]] == [
        "diagnosis",
        "mitigation_attempt_0",
    ]


def test_ids_id_based_tool_matching():
    t = _convert(IDS)
    # The first agent step issues two parallel calls, matched by id.
    step = next(s for s in t.steps if s.tool_calls and len(s.tool_calls) == 2)
    assert step.observation is not None
    matched = {r.source_call_id for r in step.observation.results}
    assert matched == {"call_d1", "call_d2"}


def test_ids_metrics_populated():
    t = _convert(IDS)
    metric_steps = [s for s in t.steps if s.metrics and s.metrics.prompt_tokens]
    assert len(metric_steps) == 4
    fm = t.final_metrics
    assert fm.total_prompt_tokens == 1200 + 1500 + 1600 + 1700
    assert fm.total_completion_tokens == 40 + 25 + 30 + 15
    assert fm.total_cached_tokens == 300  # only the first call reported cache_read


def test_ids_tool_name_preserved_in_observation():
    t = _convert(IDS)
    step = next(s for s in t.steps if s.tool_calls)
    res = step.observation.results[0]
    assert res.extra is not None and res.extra.get("tool_name")


def test_ids_roundtrips():
    t = _convert(IDS)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))


def test_sregym_meta_injected():
    t = _convert(IDS)
    assert t.extra["sregym"]["problem_id"] == "p"
    assert t.extra["sregym"]["run"] == 1
