"""No Docker needed. Adversarial outcome and fault-causality contracts."""

import copy

import pytest

from sregym.postmortems.roblox_consul.grading import evaluate
from sregym.postmortems.roblox_consul.model import TIERS, bolt_delay, streaming_delay, validate_config
from sregym.postmortems.roblox_consul.runner import Run


@pytest.fixture
def healthy():
    return dict(
        samples=[dict(success=20, failed=0, maintenance=0, incorrect=0, admission=100) for _ in range(15)],
        window=15,
        rps=20,
        rows=[{"id": 1, "coins": 1001}],
        expected=[{"id": 1, "coins": 1001}],
        overloads=0,
        initial_overloads=0,
        voters=3,
        prior_incorrect=False,
    )


def test_healthy_outcomes_pass(healthy):
    assert evaluate(**healthy)["passed"]


@pytest.mark.parametrize(
    "mutation,check",
    [
        ({"samples": []}, "observation_window"),
        ({"rows": []}, "player_data_preserved"),
        ({"rows": [{"id": 1, "coins": 0}]}, "player_data_preserved"),
        ({"overloads": 1}, "no_origin_overload"),
        ({"voters": 2}, "three_voters"),
        ({"prior_incorrect": True}, "correct_responses"),
    ],
)
def test_no_false_passes(healthy, mutation, check):
    healthy.update(mutation)
    result = evaluate(**healthy)
    assert not result["passed"]
    assert not result["checks"][check]


def test_maintenance_and_no_load_cannot_pass(healthy):
    for sample in healthy["samples"]:
        sample.update(success=0, maintenance=20, admission=0)
    assert not evaluate(**healthy)["passed"]
    for sample in healthy["samples"]:
        sample.update(success=0, maintenance=0, admission=100)
    assert not evaluate(**healthy)["checks"]["sustained_success"]


def test_transient_health_does_not_pass(healthy):
    healthy["samples"] = healthy["samples"][:2]
    assert not evaluate(**healthy)["passed"]


def test_bad_leader_recurrence_fails(healthy):
    healthy["samples"][8].update(success=0, failed=20)
    assert not evaluate(**healthy)["checks"]["sustained_success"]


@pytest.mark.parametrize("tier", TIERS.values())
def test_load_reduction_and_streaming_have_distinct_effects(tier):
    config = {
        "streaming": True,
        "routing_nodes": tier["routing_nodes"],
        "churn_per_second": tier["churn"],
        "health_interval": 1,
    }
    original = copy.deepcopy(config)
    assert streaming_delay(config, tier, False) == 0
    high = streaming_delay(config, tier, True)
    config.update(health_interval=60, routing_nodes=1, churn_per_second=1)
    assert 0.3 < streaming_delay(config, tier, True) < high
    config["streaming"] = False
    assert streaming_delay(config, tier, True) == 0
    assert bolt_delay(950_000) > 0.3  # independent second cause remains
    assert bolt_delay(0) == 0
    assert original["streaming"]


@pytest.mark.parametrize(
    "change",
    [
        {"active": False},
        {"free_pages": {}},
        {"admission_percent": -1},
        {"admission_percent": 101},
        {"admission_percent": True},
        {"streaming": "false"},
        {"routing_nodes": 0},
        {"churn_per_second": 0},
    ],
)
def test_operational_config_cannot_edit_fault_state(change):
    with pytest.raises(ValueError):
        validate_config(change, {}, TIERS["small"])


@pytest.mark.parametrize("name", ["../outside", "x/y", "../../", "", "-x", "a" * 41])
def test_run_names_cannot_escape_results(name):
    with pytest.raises(ValueError):
        Run(name)


def test_unavailable_grader_is_invalid_not_success(tmp_path, monkeypatch):
    run = Run("unavailable")
    run.root = tmp_path

    def unavailable(**kwargs):
        raise TimeoutError("evidence collection unavailable")

    monkeypatch.setattr(run, "_grade", unavailable)
    monkeypatch.setattr(run, "export", lambda: None)
    result = run.grade()
    assert result["passed"] is False
    assert result["valid"] is False
    assert (tmp_path / "grade.json").exists()


def test_trace_records_actual_model_and_cli(tmp_path):
    import json

    from sregym.postmortems.roblox_consul.benchmark import trace_metadata

    native = tmp_path / "codex-sessions"
    native.mkdir()
    records = [
        {"type": "session_meta", "payload": {"cli_version": "test", "model_provider": "openai"}},
        {"type": "turn_context", "payload": {"model": "selected-model"}},
    ]
    (native / "run.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    (tmp_path / "codex.jsonl").write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
    assert trace_metadata(tmp_path) == {
        "cli_version": "test",
        "model_provider": "openai",
        "resolved_model": "selected-model",
        "usage": {"input_tokens": 10},
    }
