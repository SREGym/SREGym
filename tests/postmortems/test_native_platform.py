"""Safety invariants beyond HTTP availability or process liveness."""

import copy

import pytest

from sregym.postmortems.roblox_platform.grading import data_checks


@pytest.fixture
def evidence():
    return {
        "shards": [
            {
                "players": [[0, "player-0", 99999]],
                "purchases": [["r1", 0, "experience-pass"]],
                "sessions": [["r1", 0]],
                "receipts": [["r1", 0]],
            }
        ],
        "acknowledgments": [{"request_id": "r1", "player": 0}],
        "players": 1,
        "must_process": {"r1"},
    }


def test_native_consistent_work(evidence):
    assert all(data_checks(**evidence).values())


def test_balances_cannot_be_reset_after_purchases(evidence):
    evidence["shards"][0]["players"][0][2] = 100000
    assert not data_checks(**evidence)["balances_conserved"]


def test_healthy_http_does_not_excuse_lost_acknowledged_work(evidence):
    evidence["shards"][0]["purchases"] = []
    evidence["shards"][0]["receipts"] = []
    evidence["shards"][0]["players"][0][2] = 100000
    assert not data_checks(**evidence)["acknowledged_work_preserved"]


def test_unacknowledged_committed_work_still_requires_receipt(evidence):
    evidence["acknowledgments"] = []
    evidence["shards"][0]["receipts"] = []
    assert not data_checks(**evidence)["recovery_tail_processed"]


def test_cross_shard_duplicate_not_hidden_by_dict_merge(evidence):
    evidence["shards"].append(copy.deepcopy(evidence["shards"][0]))
    assert not data_checks(**evidence)["unique_transactions"]


def test_receipt_must_belong_to_same_player(evidence):
    evidence["shards"][0]["receipts"][0][1] = 1
    assert not data_checks(**evidence)["consistent_transactions"]


def test_purchase_requires_durable_session(evidence):
    evidence["shards"][0]["sessions"] = []
    assert not data_checks(**evidence)["consistent_transactions"]
