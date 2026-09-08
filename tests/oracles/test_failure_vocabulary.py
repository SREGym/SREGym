"""The shared failure vocabulary, the ``Oracle.fail`` seam, and aggregation.

The per-oracle behaviour is pinned in ``test_failure_classification.py``. What
is pinned here is the machinery those twenty-eight oracles share, because a
mistake in it misclassifies every one of them at once.
"""

import pytest

from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.failure import (
    ALL_CLASSES,
    SHARED_FAILURE_CLASSES,
    FailureClass,
    classify,
    worst,
)


class _Oracle(Oracle):
    """A minimal oracle with one local reason that shadows a shared one."""

    FAILURE_CLASSES = {
        "local_only": FailureClass.AGENT_ERROR,
        # Deliberately disagrees with SHARED_FAILURE_CLASSES to prove the
        # override path is real and not merely a fallback.
        "no_ready_endpoints": FailureClass.AGENT_ERROR,
    }

    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        return {"success": True}


# ----------------------------------------------------------------------------
# classify
# ----------------------------------------------------------------------------


def test_an_unmapped_reason_is_ambiguous_rather_than_raising():
    """Classification is metadata and must never be load-bearing for a run.

    If adding a reason code could raise, the safe thing for an oracle author to
    do would be to not add one -- which defeats the point.
    """
    assert classify("a_reason_nobody_has_mapped") == FailureClass.AMBIGUOUS


def test_the_shared_table_is_consulted_when_there_is_no_override():
    assert classify("prometheus_unreachable") == FailureClass.ENVIRONMENT_ERROR


def test_an_oracle_override_beats_the_shared_table():
    assert classify("no_ready_endpoints") == FailureClass.AMBIGUOUS
    assert classify("no_ready_endpoints", _Oracle.FAILURE_CLASSES) == FailureClass.AGENT_ERROR


def test_every_shared_reason_maps_to_a_real_class():
    """Guards against a typo'd class string, which would silently never match."""
    assert set(SHARED_FAILURE_CLASSES.values()) <= ALL_CLASSES


def test_shared_reasons_are_snake_case_codes():
    """``reason`` reaches the CSV and gets filtered on, so it must stay stable."""
    for reason in SHARED_FAILURE_CLASSES:
        assert reason == reason.lower()
        assert " " not in reason


# ----------------------------------------------------------------------------
# Oracle.fail
# ----------------------------------------------------------------------------


def test_fail_resolves_against_the_subclass_not_the_base():
    """``fail`` is a classmethod so a subclass's table is in scope.

    Written as a regression test: the obvious staticmethod spelling cannot see
    ``cls.FAILURE_CLASSES`` and would classify every local reason as ambiguous.
    """
    assert _Oracle.fail("local_only")["failure_class"] == FailureClass.AGENT_ERROR


def test_fail_works_unbound_and_on_an_instance():
    instance = _Oracle.__new__(_Oracle)
    assert instance.fail("local_only") == _Oracle.fail("local_only")


def test_an_oracle_with_no_table_still_gets_shared_reasons():
    class Bare(Oracle):
        def evaluate(self, solution=None, trace=None, duration=None) -> dict:
            return {"success": True}

    assert Bare.fail("oracle_raised")["failure_class"] == FailureClass.HARNESS_ERROR


def test_fail_always_reports_failure():
    """No reason code should be able to produce an accidental pass."""
    assert _Oracle.fail("local_only")["success"] is False


# ----------------------------------------------------------------------------
# worst -- precedence
# ----------------------------------------------------------------------------


def test_an_environmental_failure_dominates_an_agent_one():
    """The core scoring rule.

    If the cluster could not host the mitigation, the fault still being present
    says nothing about the model -- so the environment wins even against a
    decisive agent failure. Getting this backwards would score models down for
    broken infrastructure, which is the whole problem being solved.
    """
    assert worst([FailureClass.AGENT_ERROR, FailureClass.ENVIRONMENT_ERROR]) == FailureClass.ENVIRONMENT_ERROR


def test_a_harness_failure_dominates_an_agent_one():
    assert worst([FailureClass.AGENT_ERROR, FailureClass.HARNESS_ERROR]) == FailureClass.HARNESS_ERROR


def test_an_environmental_failure_dominates_a_harness_one():
    assert worst([FailureClass.HARNESS_ERROR, FailureClass.ENVIRONMENT_ERROR]) == FailureClass.ENVIRONMENT_ERROR


def test_a_decisive_agent_failure_beats_a_sibling_that_could_not_tell():
    """Ambiguity should not dilute positive evidence.

    A check that confirms the injected fault is still present is decisive; a
    connectivity probe that timed out alongside it is not a reason to downgrade
    that to "cannot tell".
    """
    assert worst([FailureClass.AMBIGUOUS, FailureClass.AGENT_ERROR]) == FailureClass.AGENT_ERROR


def test_precedence_does_not_depend_on_order():
    pair = [FailureClass.AGENT_ERROR, FailureClass.ENVIRONMENT_ERROR]
    assert worst(pair) == worst(list(reversed(pair)))


def test_nothing_to_go_on_is_ambiguous():
    """Something failed or we would not be aggregating, but nothing said why."""
    assert worst([]) == FailureClass.AMBIGUOUS


@pytest.mark.parametrize("single", sorted(ALL_CLASSES))
def test_a_lone_class_survives_aggregation(single):
    assert worst([single]) == single


# ----------------------------------------------------------------------------
# CompoundedOracle propagation
# ----------------------------------------------------------------------------


class _Child(Oracle):
    def __init__(self, verdict, importance=1.0):
        self._verdict = verdict
        self.importance = importance

    def evaluate(self, *args, **kwargs):
        return dict(self._verdict)


class _Raising(Oracle):
    importance = 1.0

    def __init__(self):
        pass

    def evaluate(self, *args, **kwargs):
        raise RuntimeError("kubectl exploded")


def _compound(**children):
    compound = CompoundedOracle.__new__(CompoundedOracle)
    compound.oracles = children
    return compound


def test_a_passing_compound_grows_no_failure_columns():
    result = _compound(a=_Child({"success": True})).evaluate()
    assert result["success"] is True
    assert "failure_class" not in result
    assert "reason" not in result


def test_a_child_reason_reaches_the_top_level():
    """Otherwise the sweep is invisible for compound problems.

    The conductor merges only the top-level dict into ``self.results``, so a
    reason left inside ``oracles[]`` never reaches the CSV.
    """
    result = _compound(
        alerts=_Child({"success": False, "reason": "alerts_still_firing", "failure_class": "agent_error"})
    ).evaluate()

    assert result["failure_class"] == FailureClass.AGENT_ERROR
    assert result["reason"] == "alerts_still_firing"
    # The child stays verbatim in `oracles`, which is where per-child detail lives.
    assert result["oracles"][0]["reason"] == "alerts_still_firing"


def test_the_dominating_class_wins_across_children():
    result = _compound(
        alerts=_Child({"success": False, "reason": "alerts_still_firing", "failure_class": "agent_error"}),
        health=_Child(
            {"success": False, "reason": "required_deployment_missing", "failure_class": "environment_error"}
        ),
    ).evaluate()

    assert result["failure_class"] == FailureClass.ENVIRONMENT_ERROR
    # Both parts stay readable; only the class is collapsed.
    assert result["reason"] == "alerts_still_firing+required_deployment_missing"


def test_a_passing_child_is_not_blamed():
    """A child that succeeded must not contribute a reason or a class."""
    result = _compound(
        ok=_Child({"success": True, "reason": "should_be_ignored", "failure_class": "environment_error"}),
        bad=_Child({"success": False, "reason": "alerts_still_firing", "failure_class": "agent_error"}),
    ).evaluate()

    assert result["reason"] == "alerts_still_firing"
    assert result["failure_class"] == FailureClass.AGENT_ERROR


def test_a_raising_child_is_a_harness_error():
    result = _compound(boom=_Raising()).evaluate()

    assert result["success"] is False
    assert result["failure_class"] == FailureClass.HARNESS_ERROR
    assert result["reason"] == "oracle_raised"
    assert "RuntimeError: kubectl exploded" in result["oracles"][0]["error"]


def test_an_unexplained_child_failure_is_ambiguous_not_a_crash():
    """Oracles not yet swept still return a bare ``{"success": False}``.

    The sweep is incremental, so aggregation has to tolerate children that say
    nothing about why -- and must not guess.
    """
    result = _compound(old=_Child({"success": False})).evaluate()

    assert result["failure_class"] == FailureClass.AMBIGUOUS
    assert "reason" not in result


def test_aggregation_leaves_accuracy_alone():
    """Classification is additive; it must not perturb existing scoring."""
    children = {
        "a": _Child({"success": False, "reason": "alerts_still_firing", "failure_class": "agent_error"}),
        "b": _Child({"success": True}),
    }
    result = _compound(**children).evaluate()
    assert result["accuracy"] == pytest.approx(50.0)
