"""Failure classification on the wrong-pod-selection mitigation oracle.

A failed mitigation used to read identically whether the model got it wrong or
the cluster was broken underneath it. These tests pin the distinction, and pin
that the human-readable prints survive alongside the machine-readable reason --
they have different consumers and neither replaces the other.
"""

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.wrong_pod_selection_mitigation import (
    WrongPodSelectionMitigationOracle as Oracle,
)


@pytest.fixture
def oracle():
    """The oracle without its Kubernetes client construction."""
    return Oracle.__new__(Oracle)


def test_fail_carries_reason_and_class(oracle):
    verdict = oracle.fail("wrong_pods_selected", pods=["search-1"])
    assert verdict == {
        "success": False,
        "reason": "wrong_pods_selected",
        "failure_class": "agent_error",
        "detail": {"pods": ["search-1"]},
    }


def test_detail_is_omitted_when_there_is_nothing_to_say(oracle):
    assert "detail" not in oracle.fail("no_ready_endpoints")


def test_an_unmapped_reason_is_ambiguous_not_an_error(oracle):
    """A new reason code must never be able to fail a run."""
    assert oracle.fail("something_nobody_mapped")["failure_class"] == "ambiguous"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # The one branch that unambiguously means the fault is still present.
        ("wrong_pods_selected", "agent_error"),
        # The cluster could not host the mitigation.
        ("required_deployment_missing", "environment_error"),
        ("required_deployment_scaled_to_zero", "environment_error"),
        ("required_deployment_not_rolled_out", "environment_error"),
        # Honestly undecidable from the oracle's vantage point.
        ("no_ready_endpoints", "ambiguous"),
        ("no_active_replicaset", "ambiguous"),
        ("connectivity_probe_failed", "ambiguous"),
    ],
)
def test_every_reason_classifies_as_documented(oracle, reason, expected):
    """Pinned through ``fail`` rather than a table.

    Most of these reasons are now shared across oracles and only
    ``wrong_pods_selected`` is local, so asserting against this oracle's own
    mapping would test almost nothing. Going through ``fail`` tests the
    resolution order that actually runs.
    """
    assert oracle.fail(reason)["failure_class"] == expected


def test_this_oracle_never_reports_a_harness_error(oracle):
    """``harness_error`` is the conductor's to assign, not an oracle's.

    An oracle that is running has, by definition, not failed to run.
    """
    reasons = ["wrong_pods_selected", "required_deployment_missing", "no_ready_endpoints"]
    assert "harness_error" not in {oracle.fail(r)["failure_class"] for r in reasons}


class _Problem:
    """Just the attributes the health check reads."""

    def __init__(self, kubectl):
        self.kubectl = kubectl
        self.namespace = "hotel-reservation"
        self.frontend_service = "frontend"
        self.wrong_deployment = "search"


class _Kubectl:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def get_deployment(self, name, namespace):
        return self._behaviour(name)


def _deployment(replicas):
    class Spec:
        pass

    class Dep:
        pass

    spec, dep = Spec(), Dep()
    spec.replicas = replicas
    dep.spec = spec
    return dep


def test_a_missing_deployment_is_environmental_and_names_itself(oracle, capsys):
    def missing(_name):
        raise ApiException(status=404, reason="Not Found")

    oracle.problem = _Problem(_Kubectl(missing))
    verdict = oracle._required_deployments_unhealthy()

    assert verdict["failure_class"] == "environment_error"
    assert verdict["reason"] == "required_deployment_missing"
    assert verdict["detail"]["deployment"] == "frontend"
    # The print is the debugging path and must survive.
    assert "is missing" in capsys.readouterr().out


def test_a_scaled_to_zero_deployment_is_environmental(oracle, capsys):
    oracle.problem = _Problem(_Kubectl(lambda _n: _deployment(0)))
    verdict = oracle._required_deployments_unhealthy()

    assert verdict["reason"] == "required_deployment_scaled_to_zero"
    assert verdict["failure_class"] == "environment_error"
    assert "scaled to zero" in capsys.readouterr().out


def test_a_non_404_api_error_still_propagates(oracle):
    """A 500 is not a verdict; it belongs to the harness_error path."""

    def server_error(_name):
        raise ApiException(status=500, reason="Internal Server Error")

    oracle.problem = _Problem(_Kubectl(server_error))
    with pytest.raises(ApiException):
        oracle._required_deployments_unhealthy()


def test_healthy_deployments_return_none(oracle, monkeypatch):
    oracle.problem = _Problem(_Kubectl(lambda _n: _deployment(1)))
    monkeypatch.setattr(Oracle, "_wait_for_current_rollout", lambda self, dep: dep)
    assert oracle._required_deployments_unhealthy() is None


def test_a_stalled_rollout_is_environmental(oracle, monkeypatch, capsys):
    oracle.problem = _Problem(_Kubectl(lambda _n: _deployment(1)))
    monkeypatch.setattr(Oracle, "_wait_for_current_rollout", lambda self, dep: None)

    verdict = oracle._required_deployments_unhealthy()
    assert verdict["reason"] == "required_deployment_not_rolled_out"
    assert verdict["failure_class"] == "environment_error"
    assert "not fully rolled out" in capsys.readouterr().out


def test_a_raising_oracle_is_harness_error_not_the_model_fault():
    """The oracle never reached a verdict, so nothing is owed to the model."""
    from sregym.conductor.conductor import Conductor

    class Boom:
        def evaluate(self):
            raise RuntimeError("kubectl exploded")

    class P:
        mitigation_oracle = Boom()

    c = Conductor.__new__(Conductor)
    c.problem = P()  # current_problem is a read-only property over this
    c.logger = __import__("logging").getLogger("test.failure_class")
    c.execution_start_time = 0.0

    r = c._evaluate_mitigation("some solution")
    assert r["success"] is False
    assert r["failure_class"] == "harness_error"
    assert r["reason"] == "oracle_raised"
    assert "RuntimeError: kubectl exploded" in r["error"]


def test_a_successful_mitigation_carries_no_failure_class(oracle, monkeypatch):
    """Classification is about failures; a pass should not grow the columns."""
    from sregym.conductor.conductor import Conductor

    class Fine:
        def evaluate(self):
            return {"success": True}

    class P:
        mitigation_oracle = Fine()

    c = Conductor.__new__(Conductor)
    c.problem = P()  # current_problem is a read-only property over this
    c.logger = __import__("logging").getLogger("test.failure_class")
    c.execution_start_time = 0.0

    r = c._evaluate_mitigation("sol")
    assert r == {"success": True}
