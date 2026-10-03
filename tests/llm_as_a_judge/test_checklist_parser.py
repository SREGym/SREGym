import json

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
