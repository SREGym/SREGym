"""Before deploying, the conductor resets the cluster to its saved baseline.

A killed or stalled run skips the end-of-problem reconcile, so the next problem
must clear the earlier problem's leftovers itself.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor import conductor as conductor_module
from sregym.conductor.conductor import Conductor
from tests.conductor.test_submission_lifecycle import _conductor


def _leftover_conductor(loaded: bool) -> Conductor:
    c = Conductor.__new__(Conductor)
    c.logger = logging.getLogger("test.reconcile_leftovers")
    c._baseline_captured = False
    c.cluster_state = Mock()
    c.cluster_state.load_baseline_state.return_value = loaded
    c.cluster_state.reconcile_to_baseline.return_value = {"namespaces_deleted": ["train-ticket"]}
    return c


def test_reconciles_to_the_saved_baseline():
    c = _leftover_conductor(loaded=True)

    c.reconcile_leftovers()

    c.cluster_state.load_baseline_state.assert_called_once_with(conductor_module.CLUSTER_BASELINE_STATE_FILE)
    c.cluster_state.reconcile_to_baseline.assert_called_once()
    assert c._baseline_captured


def test_skips_without_a_saved_baseline_and_never_captures_one():
    c = _leftover_conductor(loaded=False)

    c.reconcile_leftovers()

    c.cluster_state.reconcile_to_baseline.assert_not_called()
    c.cluster_state.save_baseline_state.assert_not_called()
    c.cluster_state.capture_baseline.assert_not_called()
    assert not c._baseline_captured


def test_a_failed_reconcile_stops_the_start():
    c = _leftover_conductor(loaded=True)
    c.cluster_state.reconcile_to_baseline.side_effect = RuntimeError("kubectl failed")

    with pytest.raises(RuntimeError, match="kubectl failed"):
        c.reconcile_leftovers()


def test_start_problem_reconciles_before_deploying(monkeypatch):
    calls = []
    c = _conductor()
    c.problem_id = "problem"
    c.submission_stage = "done"
    problem = SimpleNamespace(recover_fault=lambda: None, app=SimpleNamespace(cleanup=lambda: None))
    c.problems = SimpleNamespace(get_problem_instance=lambda _problem_id: problem)
    monkeypatch.setattr(conductor_module, "DetectionOracle", lambda _problem: object())
    for name in ("dependency_check", "get_problem_stages", "_build_stage_sequence", "_preflight_active_oracles"):
        monkeypatch.setattr(c, name, lambda *_args: None)
    for name in ("fix_kubernetes", "undeploy_app", "reconcile_leftovers"):
        monkeypatch.setattr(c, name, lambda name=name: calls.append(name))

    def deploy_app():
        calls.append("deploy_app")
        raise RuntimeError("stop after deploy")

    monkeypatch.setattr(c, "deploy_app", deploy_app)

    with pytest.raises(RuntimeError, match="stop after deploy"):
        asyncio.run(c.start_problem())

    assert calls == ["fix_kubernetes", "undeploy_app", "reconcile_leftovers", "deploy_app"]
