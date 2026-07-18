"""Golden tests for the Gemini CLI -> ATIF adapter (clean port of Harbor's converter).

Uses a REAL captured run (results/0705_0304), JSONL session format.
``message_update`` / ``$rewindTo`` replay edge behaviors are covered in
test_gemini_edge_cases.py.
"""

import json
from pathlib import Path

from sregym.traces.adapters import gemini
from sregym.traces.atif import Trajectory

FIXTURES = Path(__file__).parent / "fixtures"
REAL = FIXTURES / "gemini_run"


def _convert(fixture: Path) -> Trajectory:
    return gemini.to_atif(fixture, sregym_meta={"problem_id": "p", "run": 1})


def test_real_returns_validated_trajectory():
    t = _convert(REAL)
    assert isinstance(t, Trajectory)
    assert t.schema_version == "ATIF-v1.7"
    assert t.agent.name == "gemini"
    assert t.agent.model_name == "gemini-3.5-flash"
    assert t.session_id == "fdaf509c-201f-40e3-96ab-1dc3816c36ed"


def test_real_step_shape():
    t = _convert(REAL)
    assert len(t.steps) == 70
    sources = [s.source for s in t.steps]
    assert sources.count("user") == 35
    assert sources.count("agent") == 35
    assert t.steps[0].source == "user"


def test_real_thoughts_become_reasoning():
    t = _convert(REAL)
    reasoning_steps = [s for s in t.steps if s.reasoning_content]
    assert len(reasoning_steps) == 19


def test_real_tool_calls_all_matched_by_id():
    t = _convert(REAL)
    tool_calls = sum(len(s.tool_calls or []) for s in t.steps)
    observations = sum(len(s.observation.results) for s in t.steps if s.observation)
    assert tool_calls == 34
    assert observations == 34
    # Every observation references a real tool_call_id in its own step.
    for s in t.steps:
        if s.tool_calls and s.observation:
            call_ids = {tc.tool_call_id for tc in s.tool_calls}
            for r in s.observation.results:
                if r.source_call_id is not None:
                    assert r.source_call_id in call_ids


def test_real_metrics():
    t = _convert(REAL)
    fm = t.final_metrics
    assert fm.total_prompt_tokens == 975207
    assert fm.total_completion_tokens == 9465
    assert fm.total_cached_tokens == 754649  # Gemini implicit caching (no config)
    assert fm.total_steps == 70
    # Every agent step carries metrics.
    assert sum(1 for s in t.steps if s.metrics) == 35


def test_real_roundtrips():
    t = _convert(REAL)
    Trajectory.model_validate(json.loads(json.dumps(t.to_json_dict())))


def test_real_sregym_meta_injected():
    t = _convert(REAL)
    assert t.extra["sregym"] == {"problem_id": "p", "run": 1}
