"""Existing table sizes drive traffic; no injection may mutate host limits."""

from unittest.mock import Mock

import pytest

from sregym.conductor.problems import node_conntrack_exhaustion as module


@pytest.mark.parametrize("maximum", [65536, 262144, 524288, 1048576])
def test_calibration_targets_the_existing_limit_without_any_write(monkeypatch, maximum):
    problem = object.__new__(module.NodeConntrackExhaustionHotelReservation)
    problem.victim_node, problem.namespace = "worker", "application"
    problem.kubectl = Mock()
    read = Mock(return_value=(100, maximum))
    monkeypatch.setattr(module, "read_node_conntrack_usage", read)
    problem._prepare_conntrack_limit()
    assert problem.target_connections == (maximum * 105 + 99) // 100
    assert problem.original_conntrack_max == maximum
    assert problem.gateway_port_count * 20000 >= problem.target_connections
    assert problem.kubectl.mock_calls == []
    read.assert_called_once_with(problem.kubectl, "worker", "application")


@pytest.mark.parametrize("measurements", [(0, 0), (-1, 262144), (100, -1), (262144, 262144), (100, 4194304)])
def test_invalid_saturated_or_unaffordable_tables_fail_before_creating_traffic(monkeypatch, measurements):
    problem = object.__new__(module.NodeConntrackExhaustionHotelReservation)
    problem.victim_node, problem.namespace = "worker", "application"
    problem.kubectl = Mock()
    monkeypatch.setattr(module, "read_node_conntrack_usage", Mock(return_value=measurements))
    with pytest.raises(RuntimeError):
        problem._prepare_conntrack_limit()
    assert not hasattr(problem, "target_connections")
    assert problem.kubectl.mock_calls == []
