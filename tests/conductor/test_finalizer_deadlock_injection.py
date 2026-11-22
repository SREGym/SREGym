"""Ordering and failure paths of FinalizerDeadlockController.inject_fault.

The finalizer blocks ConfigMap deletion until a live controller removes it.
If the controller is not ready when the finalizer is planted, nothing can
clear it and the namespace stays Terminating. These tests check that the
controller comes up first and that a failed injection leaves the namespace
deletable. They drive the orchestration only, not a cluster.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from sregym.conductor.problems.finalizer_deadlock_controller import (
    FinalizerDeadlockController,
    _correct_clusterrole_rules,
)

_STEPS = (
    "_delete_support_resources",
    "_create_service_account",
    "_create_clusterrole",
    "_create_clusterrolebinding",
    "_create_controller_script",
    "_deploy_controller",
    "_wait_for_controller_ready",
    "_create_finalized_configmap",
    "_replace_clusterrole",
    "_wait_for_forbidden_log",
    "_force_clear_finalizer",
)


def _problem(calls, failing=None, error=None):
    """A problem whose steps only record that they ran."""
    problem = object.__new__(FinalizerDeadlockController)
    problem.namespace = "hotel-reservation"
    problem.configmap_name = "reservation-cleanup-token"
    problem.controller_name = "cleanup-controller"
    problem.clusterrole_name = "configmap-cleanup-controller"
    problem.finalizer = "cleanup.reservations.io/pending-cleanup"
    problem.kubectl = SimpleNamespace(
        core_v1_api=SimpleNamespace(delete_namespaced_config_map=lambda *a, **k: calls.append("delete_configmap"))
    )

    for name in _STEPS:

        def step(*_args, _name=name, **_kwargs):
            calls.append(_name)
            if _name == failing:
                raise error

        setattr(problem, name, step)

    return problem


def test_controller_ready_before_finalizer():
    calls = []
    _problem(calls).inject_fault()

    assert calls.index("_wait_for_controller_ready") < calls.index("_create_finalized_configmap"), (
        "the finalizer must not be planted before a controller exists to remove it"
    )
    assert calls.index("_deploy_controller") < calls.index("_wait_for_controller_ready")


def test_readiness_timeout_plants_no_finalizer():
    """Nothing is stranded, so the namespace stays deletable and a retry works."""
    calls = []
    problem = _problem(calls, failing="_wait_for_controller_ready", error=TimeoutError("not ready"))

    with pytest.raises(TimeoutError):
        problem.inject_fault()

    assert "_create_finalized_configmap" not in calls
    assert "_force_clear_finalizer" not in calls, "there is nothing to clear"


def test_failure_restores_rbac_then_clears_finalizer():
    """The restored role lets the running controller clear the finalizer itself."""
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("no forbidden log"))

    with pytest.raises(RuntimeError):
        problem.inject_fault()

    tail = calls[calls.index("_wait_for_forbidden_log") :]
    assert tail.count("_replace_clusterrole") == 1
    assert tail.index("_replace_clusterrole") < tail.index("_force_clear_finalizer")


def test_cleanup_restores_correct_clusterrole_rules():
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("no forbidden log"))
    problem._replace_clusterrole = mock.Mock()

    with pytest.raises(RuntimeError):
        problem.inject_fault()

    assert problem._replace_clusterrole.call_args_list[-1] == mock.call(_correct_clusterrole_rules())


def test_cleanup_error_does_not_mask_original():
    """The cleanup runs from an except block. Its own error must not replace the real one."""
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("the real failure"))
    problem._force_clear_finalizer = mock.Mock(side_effect=Exception("cleanup also broke"))

    with pytest.raises(RuntimeError, match="the real failure"):
        problem.inject_fault()

    problem._force_clear_finalizer.assert_called_once()


def test_failed_clusterrole_restore_still_clears_finalizer():
    calls = []
    problem = _problem(calls)
    problem._restore_clusterrole = mock.Mock(side_effect=Exception("rbac api down"))

    problem._undo_finalizer_trap()

    assert calls == ["_force_clear_finalizer"]
