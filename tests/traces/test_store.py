"""Tests for the SQLite ATIF trajectory store."""

import json
from pathlib import Path

import pytest

from sregym.traces import store
from sregym.traces.atif import Trajectory

# Real normalized trajectories on disk (gitignored). Present when runs have been
# postprocessed locally; absent on a clean checkout — the fixture test self-skips.
_REAL_FIXTURES = sorted((Path(__file__).resolve().parents[2] / "results").rglob("trajectory.json"))


def _sample_atif(
    trajectory_id: str,
    *,
    problem_id: str = "service_port_conflict_hotel_reservation",
    agent: str = "codex",
    submitted: bool = True,
) -> dict:
    """A minimal-but-rich valid ATIF document exercising most fields."""
    return {
        "schema_version": "ATIF-v1.7",
        "trajectory_id": trajectory_id,
        "session_id": "sess-1",
        "agent": {
            "name": agent,
            "version": "1.0",
            "model_name": "test-model",
            "tool_definitions": [{"type": "function", "function": {"name": "run_shell"}}],
            "extra": {"vendor": "test"},
        },
        "steps": [
            {"step_id": 1, "source": "user", "message": "diagnose the issue"},
            {
                "step_id": 2,
                "source": "agent",
                "model_name": "test-model",
                "message": "checking",
                "reasoning_content": "let me look at the pods",
                "reasoning_effort": "high",
                "llm_call_count": 1,
                "tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "function_name": "run_shell",
                        "arguments": {"cmd": "kubectl get pods"},
                        "extra": {"timeout": 30},
                    }
                ],
                "observation": {
                    "results": [{"source_call_id": "c1", "content": "pod/foo Running", "extra": {"exit_code": 0}}]
                },
                "metrics": {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 50, "cost_usd": 0.5},
                "extra": {"note": "step-extra"},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 100,
            "total_completion_tokens": 20,
            "total_cached_tokens": 50,
            "total_cost_usd": 0.5,
            "total_steps": 2,
            "extra": {"aggregate": "yes"},
        },
        "notes": "a note",
        "extra": {
            "sregym": {
                "problem_id": problem_id,
                "run": 1,
                "results_path": f"0629_1125/{agent}/{problem_id}/run_1",
                "application": "Hotel Reservation",
                "submitted": submitted,
                "diagnosis_submitted_step": 2,
            }
        },
    }


def _sample_with_subagent(trajectory_id: str) -> dict:
    doc = _sample_atif(trajectory_id)
    doc["subagent_trajectories"] = [
        {
            "schema_version": "ATIF-v1.7",
            "trajectory_id": f"{trajectory_id}/sub-1",
            "agent": {"name": "sub", "version": "1.0"},
            "steps": [{"step_id": 1, "source": "agent", "message": "sub work"}],
        }
    ]
    return doc


def test_upsert_and_get_roundtrip_fidelity(tmp_path):
    db = tmp_path / "traces.db"
    doc = _sample_atif("t1")
    orig = Trajectory.model_validate(doc)

    store.upsert(orig, db)
    got = store.get("t1", db)

    assert got is not None
    # Reconstructed model must equal the original, byte-for-byte at the model
    # level -- proves the normalized shape drops nothing.
    assert got.model_dump(exclude_none=True, mode="json") == orig.model_dump(exclude_none=True, mode="json")


def test_get_exposes_granular_fields(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(Trajectory.model_validate(_sample_atif("t1")), db)
    got = store.get("t1", db)

    step = got.steps[1]
    assert step.reasoning_content == "let me look at the pods"
    assert step.tool_calls[0].function_name == "run_shell"
    assert step.tool_calls[0].arguments == {"cmd": "kubectl get pods"}
    assert step.observation.results[0].content == "pod/foo Running"
    assert step.metrics.prompt_tokens == 100
    assert (got.extra or {})["sregym"]["problem_id"] == "service_port_conflict_hotel_reservation"


def test_sql_without_parsing(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(Trajectory.model_validate(_sample_atif("t1")), db)
    with store.connect(db) as conn:
        # tool-call distribution straight from a table
        n = conn.execute("SELECT COUNT(*) AS n FROM tool_calls WHERE function_name = 'run_shell'").fetchone()["n"]
        assert n == 1
        # reasoning steps via SQL
        r = conn.execute("SELECT COUNT(*) AS n FROM steps WHERE reasoning_content IS NOT NULL").fetchone()["n"]
        assert r == 1
        # drill into a JSONB argument column
        cmd = conn.execute("SELECT arguments ->> '$.cmd' AS cmd FROM tool_calls").fetchone()["cmd"]
        assert cmd == "kubectl get pods"


def test_upsert_is_idempotent(tmp_path):
    db = tmp_path / "traces.db"
    traj = Trajectory.model_validate(_sample_atif("t1"))
    store.upsert(traj, db)
    store.upsert(traj, db)  # clean replace, not duplicate
    assert store.stats(db)["total"] == 1
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM steps").fetchone()["n"] == 2
        assert conn.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"] == 1


def test_subagent_nesting_roundtrip(tmp_path):
    db = tmp_path / "traces.db"
    orig = Trajectory.model_validate(_sample_with_subagent("root"))
    store.upsert(orig, db)

    got = store.get("root", db)
    assert got.subagent_trajectories is not None
    assert len(got.subagent_trajectories) == 1
    assert got.subagent_trajectories[0].trajectory_id == "root/sub-1"
    assert got.model_dump(exclude_none=True, mode="json") == orig.model_dump(exclude_none=True, mode="json")
    # Subagent is not listed as a root trajectory by default.
    assert {s.trajectory_id for s in store.query(db_path=db)} == {"root"}


def test_query_filters(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(Trajectory.model_validate(_sample_atif("a", problem_id="p1", agent="codex", submitted=True)), db)
    store.upsert(Trajectory.model_validate(_sample_atif("b", problem_id="p1", agent="gemini", submitted=False)), db)
    store.upsert(Trajectory.model_validate(_sample_atif("c", problem_id="p2", agent="codex", submitted=True)), db)

    assert {s.trajectory_id for s in store.query(problem_id="p1", db_path=db)} == {"a", "b"}
    assert {s.trajectory_id for s in store.query(agent="codex", db_path=db)} == {"a", "c"}
    assert {s.trajectory_id for s in store.query(problem_id="p1", agent="codex", db_path=db)} == {"a"}
    assert {s.trajectory_id for s in store.query(submitted=False, db_path=db)} == {"b"}


def test_stats_groups(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(Trajectory.model_validate(_sample_atif("a", problem_id="p1", agent="codex")), db)
    store.upsert(Trajectory.model_validate(_sample_atif("b", problem_id="p1", agent="gemini")), db)
    s = store.stats(db)
    assert s["total"] == 2
    assert s["by_problem"]["p1"] == 2
    assert s["by_agent"]["codex"] == 1


def test_get_missing_returns_none(tmp_path):
    db = tmp_path / "traces.db"
    store.init_db(db)
    assert store.get("nope", db) is None


def test_ingest_tree_walks_and_skips_bad(tmp_path):
    db = tmp_path / "traces.db"
    results = tmp_path / "results"
    good = results / "0629_1125" / "codex" / "p1" / "run_1"
    good.mkdir(parents=True)
    (good / "trajectory.json").write_text(json.dumps(_sample_atif("g1", problem_id="p1")), encoding="utf-8")
    bad = results / "0629_1125" / "codex" / "p2" / "run_1"
    bad.mkdir(parents=True)
    (bad / "trajectory.json").write_text("{not valid json", encoding="utf-8")

    stored = store.ingest_tree(results, db)
    assert stored == ["g1"]
    assert store.stats(db)["total"] == 1


@pytest.mark.skipif(not _REAL_FIXTURES, reason="no results/**/trajectory.json on disk (gitignored)")
@pytest.mark.parametrize("fixture", _REAL_FIXTURES, ids=lambda p: p.parent.relative_to(p.parents[4]).as_posix())
def test_roundtrip_real_fixtures(tmp_path, fixture):
    """Every real normalized trajectory must round-trip byte-identically.

    Encodes the plan's headline guarantee ("reconstructs exactly over all real
    trajectories") as a repeatable test, so a store.py regression against real
    agent shapes is caught, not just synthetic ones.
    """
    db = tmp_path / "traces.db"
    orig = Trajectory.model_validate(json.loads(fixture.read_text(encoding="utf-8")))
    store.upsert(orig, db)
    got = store.get(orig.trajectory_id, db)
    assert got is not None
    assert got.model_dump(exclude_none=True, mode="json") == orig.model_dump(exclude_none=True, mode="json")


def _sample_atif_all_leaves(trajectory_id: str = "full") -> dict:
    """One doc touching every JSONB column + coercion path, so CI (not just the
    gitignored real fixtures) exercises multimodal parts, per-token arrays,
    subagent refs, is_copied_context, llm_call_count=0 dispatch, and
    continued_trajectory_ref."""
    return {
        "schema_version": "ATIF-v1.7",
        "trajectory_id": trajectory_id,
        "agent": {
            "name": "codex",
            "version": "1.0",
            "tool_definitions": [{"type": "function", "function": {"name": "f"}}],
            "extra": {"v": 1},
        },
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": [
                    {"type": "text", "text": "look"},
                    {"type": "image", "source": {"media_type": "image/png", "path": "a.png"}},
                ],
            },
            {
                "step_id": 2,
                "source": "agent",
                "message": "ok",
                "is_copied_context": True,
                "llm_call_count": 2,
                "reasoning_content": "think",
                "tool_calls": [
                    {"tool_call_id": "c1", "function_name": "f", "arguments": {"a": 1}, "extra": {"timeout": 30}}
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "c1",
                            "content": [
                                {"type": "text", "text": "out"},
                                {"type": "image", "source": {"media_type": "image/jpeg", "path": "b.jpg"}},
                            ],
                            "extra": {"score": 0.9},
                        },
                        {
                            "source_call_id": None,
                            "subagent_trajectory_ref": [
                                {"trajectory_id": "sub-x", "session_id": "s", "extra": {"k": "v"}}
                            ],
                        },
                    ]
                },
                "metrics": {
                    "prompt_tokens": 10,
                    "prompt_token_ids": [1, 2, 3],
                    "completion_token_ids": [4, 5],
                    "logprobs": [-0.1, -0.2],
                    "extra": {"m": 1},
                },
                "extra": {"s": 1},
            },
            {"step_id": 3, "source": "agent", "message": "dispatch", "llm_call_count": 0, "is_copied_context": False},
        ],
        "continued_trajectory_ref": "next.json",
        "final_metrics": {"total_steps": 3, "extra": {"agg": 1}},
        "extra": {"sregym": {"problem_id": "p1", "run": 1}},
    }


def test_all_variable_leaves_roundtrip(tmp_path):
    """CI guard: every JSONB column + coercion path round-trips byte-identically.

    Runs unconditionally (unlike the gitignored real-fixture test), so a
    regression in any variable-leaf column is caught on a clean checkout.
    """
    db = tmp_path / "traces.db"
    orig = Trajectory.model_validate(_sample_atif_all_leaves())
    store.upsert(orig, db)
    got = store.get("full", db)
    assert got is not None
    assert got.model_dump(exclude_none=True, mode="json") == orig.model_dump(exclude_none=True, mode="json")


def test_subagent_array_order_preserved(tmp_path):
    """Subagents must reconstruct in their original array order, not id-sorted."""
    db = tmp_path / "traces.db"
    doc = {
        "schema_version": "ATIF-v1.7",
        "trajectory_id": "root",
        "agent": {"name": "a", "version": "1"},
        "steps": [{"step_id": 1, "source": "agent", "message": "x"}],
        "subagent_trajectories": [
            {
                "schema_version": "ATIF-v1.7",
                "trajectory_id": "zzz",
                "agent": {"name": "s", "version": "1"},
                "steps": [{"step_id": 1, "source": "agent", "message": "z"}],
            },
            {
                "schema_version": "ATIF-v1.7",
                "trajectory_id": "aaa",
                "agent": {"name": "s", "version": "1"},
                "steps": [{"step_id": 1, "source": "agent", "message": "a"}],
            },
        ],
    }
    orig = Trajectory.model_validate(doc)
    store.upsert(orig, db)
    got = store.get("root", db)
    assert [s.trajectory_id for s in got.subagent_trajectories] == ["zzz", "aaa"]
    assert got.model_dump(exclude_none=True, mode="json") == orig.model_dump(exclude_none=True, mode="json")


def test_nested_subagent_roundtrip(tmp_path):
    """Depth-2 nesting (root -> sub -> inner) round-trips with correct linkage."""
    db = tmp_path / "traces.db"
    doc = {
        "schema_version": "ATIF-v1.7",
        "trajectory_id": "root",
        "agent": {"name": "a", "version": "1"},
        "steps": [{"step_id": 1, "source": "agent", "message": "x"}],
        "subagent_trajectories": [
            {
                "schema_version": "ATIF-v1.7",
                "trajectory_id": "root/a",
                "agent": {"name": "s", "version": "1"},
                "steps": [{"step_id": 1, "source": "agent", "message": "mid"}],
                "subagent_trajectories": [
                    {
                        "schema_version": "ATIF-v1.7",
                        "trajectory_id": "root/a/inner",
                        "agent": {"name": "s2", "version": "1"},
                        "steps": [{"step_id": 1, "source": "agent", "message": "deep"}],
                    }
                ],
            }
        ],
    }
    orig = Trajectory.model_validate(doc)
    store.upsert(orig, db)
    got = store.get("root", db)
    assert got.subagent_trajectories[0].subagent_trajectories[0].trajectory_id == "root/a/inner"
    assert got.model_dump(exclude_none=True, mode="json") == orig.model_dump(exclude_none=True, mode="json")


def test_query_include_subagents(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(Trajectory.model_validate(_sample_with_subagent("root")), db)
    # Default: only root trajectories.
    assert {s.trajectory_id for s in store.query(db_path=db)} == {"root"}
    # include_subagents surfaces the embedded subagent row too.
    assert {s.trajectory_id for s in store.query(include_subagents=True, db_path=db)} == {"root", "root/sub-1"}


def test_get_subagent_directly(tmp_path):
    db = tmp_path / "traces.db"
    store.upsert(Trajectory.model_validate(_sample_with_subagent("root")), db)
    sub = store.get("root/sub-1", db)
    assert sub is not None
    assert sub.trajectory_id == "root/sub-1"


def test_stats_empty_db(tmp_path):
    db = tmp_path / "traces.db"
    store.init_db(db)
    assert store.stats(db) == {"total": 0, "by_problem": {}, "by_agent": {}}
