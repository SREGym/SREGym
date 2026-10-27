from unittest.mock import patch

import pytest

from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment_ports import PORTED_PROBLEMS


@pytest.mark.parametrize("problem_id,factory", sorted(PORTED_PROBLEMS.items()))
def test_port_initializes_its_actual_parent_with_the_selected_app(offline_port_cluster, problem_id, factory):
    # Track real constructor calls rather than substituting a fake constructor.
    calls = []
    original = Problem.__init__

    def initialize(problem, app, namespace=None):
        calls.append((problem, app))
        original(problem, app, namespace)

    with patch.object(Problem, "__init__", initialize):
        problem = factory()

    assert calls == [(problem, problem.app)]
    assert problem.namespace == problem.app.namespace
    assert problem.namespace in {"frappe", "saleor", "slack-spine"}
    assert problem.fault_injected is False
    assert problem.results == {}
    assert problem.baseline_duration_s == 120
    assert problem.app.wrk is not None

    # Native fault parents keep ownership of their recovery state. Their
    # initialization must occur even when a port changes the target fields.
    parent = type(problem).__mro__[1]
    if parent is not Problem and parent.__module__.startswith("sregym.conductor.problems."):
        calls = []
        original_parent = parent.__init__

        def initialize_parent(instance, *args, **kwargs):
            calls.append(instance)
            original_parent(instance, *args, **kwargs)

        with patch.object(parent, "__init__", initialize_parent):
            second = factory()
        assert calls == [second]
