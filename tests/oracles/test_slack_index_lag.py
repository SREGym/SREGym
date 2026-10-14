from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor.problems.ia_ports.slack_b import IndexLaneLagOracle


def problem(*, lag=0, indexed=("first", "second", "fresh"), backlog=("first", "second")):
    return SimpleNamespace(
        CONSUMER_GROUP="index",
        TOPIC="jobs.index",
        backlog_ids=list(backlog),
        total_lag=Mock(return_value=lag),
        send_probes=Mock(return_value=["fresh"]),
        search=Mock(side_effect=lambda ids, **kwargs: set(ids) & set(indexed)),
    )


@pytest.mark.parametrize("lag", [0, 10, 25])
def test_drained_backlog_and_fresh_search_pass(lag):
    result = IndexLaneLagOracle(problem(lag=lag)).evaluate()
    assert result["success"] is True
    assert result["backlog_count"] == 2


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"lag": 26}, "lag_not_drained"),
        ({"indexed": ("first", "second")}, "message_not_indexed"),
        ({"indexed": ("fresh",)}, "accepted_messages_missing"),
        ({"indexed": ("first", "fresh")}, "accepted_messages_missing"),
        ({"backlog": ()}, "backlog_not_recorded"),
    ],
)
def test_lag_freshness_and_original_work_are_required(kwargs, reason):
    result = IndexLaneLagOracle(problem(**kwargs)).evaluate()
    assert result["success"] is False
    assert result["reason"] == reason


@pytest.mark.parametrize("after_fresh_probe", [False, True])
def test_search_error_returns_failure(after_fresh_probe):
    state = problem()
    error = RuntimeError("search unavailable")
    state.search.side_effect = [{"fresh"}, error] if after_fresh_probe else error
    result = IndexLaneLagOracle(state).evaluate()
    assert result["success"] is False
    assert result["reason"] == "search_unavailable"
