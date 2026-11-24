import pytest

import sregym.conductor.problems.node_conntrack_exhaustion as conntrack_module
from sregym.conductor.problems.node_conntrack_exhaustion import NodeConntrackExhaustionHotelReservation


def _problem():
    problem = object.__new__(NodeConntrackExhaustionHotelReservation)
    problem.kubectl = object()
    problem.namespace = "hotel-reservation"
    problem.victim_node = "kind-worker"
    problem.original_conntrack_max = problem.target_connections = None
    problem.conntrack_max_changed = False
    return problem


PERMISSION_DENIED = "sh: can't create /proc/sys/net/netfilter/nf_conntrack_max: Permission denied"


def _node(monkeypatch, maximum, writable, error=PERMISSION_DENIED):
    state = {"maximum": maximum, "writes": []}

    def read(kubectl, node, namespace):
        return 100, state["maximum"]

    def write(kubectl, node, value, namespace):
        state["writes"].append(value)
        if writable:
            state["maximum"] = value
        else:
            raise RuntimeError(error)

    monkeypatch.setattr(conntrack_module, "read_node_conntrack_usage", read)
    monkeypatch.setattr(conntrack_module, "write_node_conntrack_max", write)
    return state


def test_a_refused_write_explains_itself_and_leaves_nothing_to_restore(monkeypatch):
    state = _node(monkeypatch, 1048576, writable=False)
    problem = _problem()

    with pytest.raises(RuntimeError, match="Set net.netfilter.nf_conntrack_max to 262144 or less on the host"):
        problem._prepare_conntrack_limit()

    assert not problem.conntrack_max_changed
    problem._restore_conntrack_limit()
    assert state["writes"] == [262144]


def test_any_other_write_failure_still_restores_the_limit(monkeypatch):
    state = _node(monkeypatch, 1048576, writable=False, error="Node healthcheck pod finished in phase Pending: ")
    problem = _problem()

    with pytest.raises(RuntimeError, match="phase Pending"):
        problem._prepare_conntrack_limit()

    assert problem.conntrack_max_changed
    with pytest.raises(RuntimeError):
        problem._restore_conntrack_limit()
    assert state["writes"] == [262144, 1048576]


def test_a_successful_write_is_restored(monkeypatch):
    state = _node(monkeypatch, 1048576, writable=True)
    problem = _problem()

    problem._prepare_conntrack_limit()

    assert problem.conntrack_max_changed
    assert problem.target_connections == 275252
    problem._restore_conntrack_limit()
    assert state["writes"] == [262144, 1048576]
    assert not problem.conntrack_max_changed


def test_a_limit_at_or_below_the_cap_is_not_written(monkeypatch):
    state = _node(monkeypatch, 262144, writable=False)
    problem = _problem()

    problem._prepare_conntrack_limit()

    assert state["writes"] == []
    assert not problem.conntrack_max_changed
