"""Judge replies survive publication without becoming oversized CSV cells."""

import copy
import csv
import json
from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.results.report import build_report
from sregym.results.resume import complete_resume_rows
from sregym.run_artifacts import ArtifactFinalizationError, RunArtifacts, stabilize_run_csv_headers


def new_run(tmp_path, attempt=1):
    return RunArtifacts.create(
        staging_root=tmp_path / "staging",
        results_root=tmp_path / "results",
        problem_id="test_fault",
        agent="codex",
        attempt=attempt,
    )


class MalformedBackend:
    model_name = "scripted-judge"

    def __init__(self, raw):
        self.raw = raw

    def inference(self, _messages):
        return SimpleNamespace(content=self.raw)


@pytest.mark.parametrize("causes", [1, 3])
@pytest.mark.parametrize("reply_format", ["plain", "invalid_question_id"])
def test_long_replies_publish_losslessly_and_keep_results_readable(tmp_path, causes, reply_format):
    raw = "test_fault: explanation instead of JSON, with Unicode 日本語\n" * 6000
    if reply_format == "invalid_question_id":
        raw = json.dumps([{"id": "test_fault-unknown-question-" * 10000, "answer": "Yes"}])
    assert len(raw) > csv.field_size_limit()
    oracle = LLMAsAJudgeOracle(None, "wrong port" if causes == 1 else [f"wrong port {i}" for i in range(causes)])
    oracle.judge._backend = MalformedBackend(raw)
    result = oracle.evaluate("checkout has the wrong port")
    snapshot = {
        "problem_id": "test_fault",
        "attempt": 1,
        "run_status": "incomplete",
        "incomplete_reason": "unscored_stage_results",
        "Mitigation.success": True,
        **{f"Diagnosis.{key}": value for key, value in result.items()},
    }
    original = copy.deepcopy(snapshot)
    run = new_run(tmp_path)

    row = run.save_judge_responses(snapshot)
    assert snapshot == original
    assert not run.judge_responses_path.is_relative_to(run.active_dir)
    destination = run.finalize_and_publish(snapshot=row, fieldnames=sorted(row))

    replies = json.loads((destination / "judge_responses.json").read_text())
    assert len(replies) == causes
    # The judge normalizes surrounding whitespace before parsing its reply.
    assert all(reply == raw.strip() for reply in replies.values())
    assert not run.judge_responses_path.exists()
    with (destination / "test_fault_results.csv").open(newline="") as stream:
        saved = next(csv.DictReader(stream))
    assert saved["Diagnosis.success"] == ""
    assert "explanation instead of JSON" not in json.dumps(saved)
    assert "judge_responses.json#Diagnosis" in json.dumps(saved)
    assert complete_resume_rows([saved], requested_attempts=1) == {}
    report = build_report([saved], problem_id="test_fault", model="test", harness="Codex", requested_attempts=1)
    assert "**Inconclusive**" in report
    assert "**Diagnosis pass rate:** n/a" in report
    assert stabilize_run_csv_headers(tmp_path / "results").overflow_dropped == []


@pytest.mark.parametrize("raw", ["", "small malformed reply"])
def test_empty_and_short_replies_use_the_same_artifact_contract(tmp_path, raw):
    run = new_run(tmp_path)
    snapshot = {"problem_id": run.problem_id, "Diagnosis.judge_raw_response": raw}

    row = run.save_judge_responses(snapshot)
    destination = run.finalize_and_publish(snapshot=row, fieldnames=list(row))

    if raw:
        assert json.loads((destination / "judge_responses.json").read_text()) == {"Diagnosis.judge_raw_response": raw}
        assert row["Diagnosis.judge_raw_response_artifact"] == "judge_responses.json#Diagnosis.judge_raw_response"
    else:
        assert not (destination / "judge_responses.json").exists()
        assert row["Diagnosis.judge_raw_response_artifact"] == ""


def test_scored_results_do_not_create_a_judge_response_artifact(tmp_path):
    run = new_run(tmp_path)
    snapshot = {"problem_id": run.problem_id, "Diagnosis.success": False, "Diagnosis.accuracy": 0.0}

    row = run.save_judge_responses(snapshot)
    destination = run.finalize_and_publish(snapshot=row, fieldnames=list(row))

    assert row == snapshot
    assert not (destination / "judge_responses.json").exists()


def test_mixed_campaign_headers_preserve_scored_and_unscored_results(tmp_path):
    snapshots = [
        {"Diagnosis.success": False, "Diagnosis.accuracy": 0.0},
        {
            "Diagnosis.success": None,
            "Diagnosis.unscored_reason": "judge_parse_failure",
            "Diagnosis.judge_raw_response": "bad",
        },
    ]
    rows = []
    paths = []
    for attempt, snapshot in enumerate(snapshots, 1):
        run = new_run(tmp_path, attempt)
        row = run.save_judge_responses({"problem_id": run.problem_id, "attempt": attempt, **snapshot})
        rows.append(row)
        fields = sorted({key for saved in rows for key in saved})
        destination = run.finalize_and_publish(snapshot=row, fieldnames=fields)
        paths.append(destination / "test_fault_results.csv")

    result = stabilize_run_csv_headers(tmp_path / "results")

    assert result.rewritten == [paths[0]]
    saved = []
    for path in paths:
        with path.open(newline="") as stream:
            saved.append(next(csv.DictReader(stream)))
    assert saved[0]["Diagnosis.success"] == "False"
    assert saved[0]["Diagnosis.accuracy"] == "0.0"
    assert saved[0]["Diagnosis.judge_raw_response_artifact"] == ""
    assert saved[1]["Diagnosis.success"] == ""
    assert saved[1]["Diagnosis.judge_raw_response_artifact"].startswith("judge_responses.json#")


def test_failed_publication_retains_host_judge_reply(tmp_path):
    run = new_run(tmp_path)
    row = run.save_judge_responses({"problem_id": run.problem_id, "Diagnosis.judge_raw_response": "malformed"})
    (run.active_dir / "agent.txt").write_text(run.problem_id)

    with pytest.raises(ArtifactFinalizationError, match="real problem id"):
        run.finalize_and_publish(snapshot=row, fieldnames=list(row))

    assert json.loads(run.judge_responses_path.read_text()) == {"Diagnosis.judge_raw_response": "malformed"}


@pytest.mark.parametrize("failure", ["corrupt_sidecar", "output_directory"])
def test_unreadable_judge_artifact_is_an_explicit_publication_failure(tmp_path, failure):
    run = new_run(tmp_path)
    row = run.save_judge_responses({"problem_id": run.problem_id, "Diagnosis.judge_raw_response": "malformed"})
    if failure == "corrupt_sidecar":
        run.judge_responses_path.write_text("{truncated")
    else:
        (run.active_dir / "judge_responses.json").mkdir()

    with pytest.raises(ArtifactFinalizationError):
        run.finalize_and_publish(snapshot=row, fieldnames=list(row))

    assert run.judge_responses_path.exists()
    assert run.active_dir.exists()
    assert not run.final_dir.exists()


def test_judge_reply_publication_does_not_follow_agent_symlinks(tmp_path):
    run = new_run(tmp_path)
    row = run.save_judge_responses({"problem_id": run.problem_id, "Diagnosis.judge_raw_response": "malformed"})
    outside = tmp_path / "outside.json"
    outside.write_text("original")
    (run.active_dir / "judge_responses.json").symlink_to(outside)

    destination = run.finalize_and_publish(snapshot=row, fieldnames=list(row))

    assert outside.read_text() == "original"
    assert not (destination / "judge_responses.json").is_symlink()
    assert json.loads((destination / "judge_responses.json").read_text()) == {
        "Diagnosis.judge_raw_response": "malformed"
    }
