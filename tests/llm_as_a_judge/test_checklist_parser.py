import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.oracles.llm_as_a_judge.judge import (
    ChecklistParseError,
    DiagnosisJudge,
)

EXPECTED_IDS = ["D1-Q1", "D1-Q2"]


def _response():
    return [
        {"id": "D1-Q1", "answer": "Yes", "evidence": "cause", "confidence": "High"},
        {"id": "D1-Q2", "answer": "No", "evidence": "scope", "confidence": "Medium"},
    ]


def test_parser_accepts_valid_json_array():
    payload = json.dumps(_response())
    assert DiagnosisJudge._parse_response(payload, EXPECTED_IDS) == _response()


def test_parser_accepts_fenced_json_with_preamble():
    payload = "I checked the evidence first.\n```json\n" + json.dumps(_response()) + "\n```"
    assert DiagnosisJudge._parse_response(payload, EXPECTED_IDS) == _response()


def test_parser_accepts_complete_array_before_second_candidate():
    payload = json.dumps(_response()) + "\n\nAlternative candidate:\n" + json.dumps(_response()[:1])
    assert DiagnosisJudge._parse_response(payload, EXPECTED_IDS) == _response()


def test_parser_ignores_bracketed_preamble():
    payload = "Reasoning [not JSON]\n" + json.dumps(_response())
    assert DiagnosisJudge._parse_response(payload, EXPECTED_IDS) == _response()


def test_parser_rejects_missing_questions():
    payload = json.dumps(_response()[:1])

    with pytest.raises(ChecklistParseError, match="Missing"):
        DiagnosisJudge._parse_response(payload, EXPECTED_IDS)


def test_parser_does_not_merge_separate_partial_arrays():
    payload = "\n".join(json.dumps([item]) for item in _response())

    with pytest.raises(ChecklistParseError, match="Missing"):
        DiagnosisJudge._parse_response(payload, EXPECTED_IDS)


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("", "\nEvidence [verified]."),
        ("[1, 2, 3]\n", ""),
        (json.dumps(_response()[:1]) + "\nFinal:\n", ""),
        ('[{"id": ["unrelated"]}]\nFinal:\n', ""),
        ('[{"id": {"unrelated": true}}]\nFinal:\n', ""),
        ('{"example": ' + json.dumps(_response()) + "}\nFinal:\n", ""),
    ],
)
def test_parser_selects_valid_candidate_without_combining_responses(prefix, suffix):
    assert DiagnosisJudge._parse_response(prefix + json.dumps(_response()) + suffix, EXPECTED_IDS) == _response()


@pytest.mark.parametrize(
    "replacement",
    [
        {"id": ["D1-Q1"]},
        {"id": {"question": "D1-Q1"}},
        {"id": None},
        {"id": "unknown"},
        {"answer": True},
        {"answer": "Maybe"},
        {"evidence": []},
        {"confidence": None},
    ],
)
def test_parser_rejects_invalid_fields_with_retryable_error(replacement):
    rows = _response()
    rows[0].update(replacement)
    with pytest.raises(ChecklistParseError):
        DiagnosisJudge._parse_response(json.dumps(rows), EXPECTED_IDS)


@pytest.mark.parametrize(
    "rows",
    [
        [*_response(), _response()[0]],
        [*_response(), "not an object"],
        [{"id": "D1-Q1"}, _response()[1]],
    ],
)
def test_parser_rejects_invalid_checklist_structure(rows):
    with pytest.raises(ChecklistParseError):
        DiagnosisJudge._parse_response(json.dumps(rows), EXPECTED_IDS)


@pytest.mark.parametrize("truncated", [False, True])
def test_parser_never_scores_nested_evidence(truncated):
    rows = [{"id": "D1-Q1", "answer": "No", "evidence": _response(), "confidence": "Low"}]
    payload = json.dumps(rows)
    if truncated:
        payload = payload[:-1]
    with pytest.raises(ChecklistParseError):
        DiagnosisJudge._parse_response(payload, EXPECTED_IDS)


def test_parser_skips_nested_examples_before_valid_final_checklist():
    rows = [{"id": "D1-Q1", "answer": "No", "evidence": _response(), "confidence": "Low"}]
    payload = json.dumps(rows) + "\nFinal:\n" + json.dumps(_response())
    assert DiagnosisJudge._parse_response(payload, EXPECTED_IDS) == _response()


@pytest.mark.parametrize("invalid", ["[" + "9" * 5000 + "]", "[" * 1100 + "]" * 1100])
def test_parser_skips_oversized_or_deeply_nested_invalid_candidates(invalid):
    with pytest.raises(ChecklistParseError):
        DiagnosisJudge._parse_response(invalid, EXPECTED_IDS)
    payload = invalid + "\nFinal:\n" + json.dumps(_response())
    assert DiagnosisJudge._parse_response(payload, EXPECTED_IDS) == _response()


@pytest.mark.parametrize("prefix,suffix", [("[}", "]"), ("[{]", "]"), ('[{"evidence": ', "")])
def test_parser_does_not_promote_nested_array_after_broken_outer_container(prefix, suffix):
    with pytest.raises(ChecklistParseError):
        DiagnosisJudge._parse_response(prefix + json.dumps(_response()) + suffix, EXPECTED_IDS)


@pytest.mark.parametrize("wrapper", [lambda rows: [rows], lambda rows: {"evidence": rows}, json.dumps])
def test_parser_rejects_wrapped_or_quoted_checklists(wrapper):
    with pytest.raises(ChecklistParseError):
        DiagnosisJudge._parse_response(json.dumps(wrapper(_response())), EXPECTED_IDS)


def test_parser_preserves_strings_and_existing_optional_field_defaults():
    rows = _response()
    rows[0]["answer"] = " YES "
    rows[0]["evidence"] = 'Logs contain [retry], {port}, "quote", \\path and ```json\ncode\n```.'
    del rows[1]["evidence"]
    del rows[1]["confidence"]
    assert DiagnosisJudge._parse_response(json.dumps(rows), EXPECTED_IDS) == rows


@pytest.mark.parametrize("malformed", ["invalid_id", "nested_evidence", "missing_question"])
def test_grading_retries_malformed_checklist_before_scoring(malformed):
    judge = DiagnosisJudge()
    rows = [{"id": qid, "answer": "Yes", "evidence": "cause", "confidence": "High"} for qid in judge._all_question_ids]
    invalid = {
        "invalid_id": [{"id": ["unrelated"]}],
        "nested_evidence": [{"id": rows[0]["id"], "answer": "No", "evidence": rows}],
        "missing_question": rows[:1],
    }[malformed]
    backend = Mock(model_name="checklist-test")
    backend.inference.side_effect = [
        SimpleNamespace(content=json.dumps(invalid)),
        SimpleNamespace(content=json.dumps(rows)),
    ]
    judge._backend = backend

    report = judge.judge_detailed("checkout has a misconfigured port", "checkout has a misconfigured port")

    assert report.composite_score == 1.0
    assert backend.inference.call_count == 2


def test_grading_preserves_retry_exhaustion_behavior():
    judge = DiagnosisJudge()
    backend = Mock(model_name="checklist-test")
    backend.inference.return_value = SimpleNamespace(content="[]")
    judge._backend = backend

    report = judge.judge_detailed("checkout has a misconfigured port", "checkout has a misconfigured port")

    assert report.composite_score == 0.0
    assert backend.inference.call_count == 2
    assert all(q.evidence == "parse failure — defaulting to No" for d in report.dimensions for q in d.questions)
