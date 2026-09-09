"""Properties that must hold across every swept oracle.

The per-oracle behaviour is tested next to each oracle. What is tested here is
the sweep *as a whole*: that no oracle still fails without saying why, that the
reason vocabulary stayed a closed set of codes, and that the two shared helpers
every oracle now leans on behave.

These are the tests that would catch the sweep rotting -- a new oracle added
next month with a bare ``{"success": False}``, or a reason code spelled two
ways in two files.
"""

import ast
import inspect
import pkgutil
from pathlib import Path

import pytest
from kubernetes.client.rest import ApiException

import sregym.conductor.oracles as oracles_pkg
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import (
    ALL_CLASSES,
    SHARED_FAILURE_CLASSES,
    FailureClass,
)

ORACLES_DIR = Path(oracles_pkg.__file__).parent

# ``base`` builds the verdict dict and ``compound`` aggregates children, so they
# are where the literal legitimately lives. Everywhere else it is an oracle that
# failed without saying why.
_VERDICT_MACHINERY = {"base", "compound"}


def _oracle_modules():
    return sorted(m.name for m in pkgutil.iter_modules([str(ORACLES_DIR)]) if not m.ispkg)


def _module_source(name):
    return (ORACLES_DIR / f"{name}.py").read_text()


# ----------------------------------------------------------------------------
# The sweep is complete, and stays complete
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("module", [m for m in _oracle_modules() if m not in _VERDICT_MACHINERY])
def test_no_oracle_reports_failure_without_a_reason(module):
    """A regression guard for the next oracle somebody writes.

    Before the sweep, 127 sites across 39 files returned a bare failure. Both
    spellings are checked: the dict literal and the ``results["success"] =
    False`` assignment, which is how ``MitigationOracle`` -- the base class 68
    problems use -- hid from the first search for this.
    """
    source = _module_source(module)
    assert '"success": False' not in source, (
        f"{module} returns a bare failure verdict; use self.fail(reason) so the "
        f"results CSV records why, and whose fault it was"
    )
    assert '"success"] = False' not in source, f"{module} assigns a bare failure verdict; use self.fail(reason) instead"


def test_every_oracle_failure_class_table_is_valid():
    """Guards against a typo'd class string, which would never match anything."""
    for module in _oracle_modules():
        source = _module_source(module)
        if "FAILURE_CLASSES" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "FAILURE_CLASSES" for t in node.targets)
            ):
                continue
            assert isinstance(node.value, ast.Dict), f"{module}: FAILURE_CLASSES must be a literal dict"
            for key in node.value.keys:
                assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                    f"{module}: FAILURE_CLASSES keys must be string literals"
                )
                reason = key.value
                assert reason == reason.lower() and " " not in reason, (
                    f"{module}: reason {reason!r} is not a snake_case code; `reason` reaches "
                    f"the results CSV and gets filtered on"
                )


def _oracle_classes():
    """Every real oracle class, found by walking the subclass tree.

    Restricted to classes defined under ``sregym`` on purpose: other test
    modules define their own Oracle subclasses to exercise the machinery -- one
    of them overrides a shared reason deliberately -- and those would otherwise
    show up here depending on test import order.
    """

    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    # Importing the package's modules populates __subclasses__.
    for module in _oracle_modules():
        try:
            __import__(f"sregym.conductor.oracles.{module}")
        except Exception:  # pragma: no cover - optional deps
            continue
    return [c for c in walk(Oracle) if c.__module__.startswith("sregym.")]


def _all_subclass_tables():
    """Every oracle's *effective* table, including anything inherited."""
    return {c.__name__: c.FAILURE_CLASSES for c in _oracle_classes() if c.FAILURE_CLASSES}


def _declared_tables():
    """Only tables written on the class itself.

    Distinct from ``_all_subclass_tables`` because inheritance is load-bearing
    here: the oracles that subclass ``MitigationOracle`` inherit its
    baseline-backed view of a missing Deployment, and should not have to repeat
    it.
    """
    return {c.__name__: c.__dict__["FAILURE_CLASSES"] for c in _oracle_classes() if "FAILURE_CLASSES" in c.__dict__}


def test_no_oracle_invents_a_class_outside_the_closed_set():
    tables = _all_subclass_tables()
    assert tables, "expected at least one oracle to declare a table"
    for name, table in tables.items():
        unknown = set(table.values()) - ALL_CLASSES
        assert not unknown, f"{name} uses classes outside the closed set: {unknown}"


def test_no_oracle_claims_a_harness_error_for_itself():
    """``harness_error`` means "we never got a verdict".

    An oracle that is running has by definition not failed to run, so it should
    never classify its *own* checks that way. The two legitimate producers are
    the conductor's exception handler and ``fail_from_exception``.
    """
    allowed = {
        # capture_baseline not having run is a sequencing failure, not a check.
        "SearchRateRetryMitigationOracle",
        # Our probe patch not taking effect means the test never ran.
        "RollingUpdateMitigationOracle",
        # No probe pod means nothing was measured.
        "FeatureFlagHttpProbeMitigationOracle",
        # The problem never recorded the offset to measure against.
        "DataPlaneProgressOracle",
    }
    for name, table in _all_subclass_tables().items():
        if name in allowed:
            continue
        assert FailureClass.HARNESS_ERROR not in table.values(), (
            f"{name} classifies one of its own checks as harness_error"
        )


def test_a_reason_is_not_classified_two_ways_without_a_stated_reason():
    """Overrides of a shared reason are deliberate, and few.

    Each of these overrides exists because that oracle has evidence the shared
    table cannot assume -- a pre-injection baseline, or a fault that sits inside
    the check's own blast radius. Adding one should be a decision, so this test
    fails when the list grows.
    """
    expected_overrides = {
        # Both record a *before* via capture_baseline, so a Deployment that was
        # there and now is not is attributable rather than ambiguous.
        ("MitigationOracle", "required_deployment_missing"),
        ("SearchRateRetryMitigationOracle", "required_deployment_missing"),
        # The agent rewrites these Deployments as its mitigation, so a stalled
        # rollout is not an environmental signal here.
        ("RollingUpdateMitigationOracle", "required_deployment_not_rolled_out"),
        ("EdgeRequestFilterMitigationOracle", "required_deployment_not_rolled_out"),
        # This problem's fault *is* the lock, so it states its own view of
        # fault_still_present rather than relying on the shared entry.
        ("PostgresLockMitigationOracle", "fault_still_present"),
    }
    found = {
        (name, reason)
        for name, table in _declared_tables().items()
        for reason in table
        if reason in SHARED_FAILURE_CLASSES
    }
    unexpected = found - expected_overrides
    assert not unexpected, (
        f"new overrides of shared reason codes: {sorted(unexpected)}. Each needs a "
        f"stated justification -- the shared classification is the default for a reason"
    )


def test_mitigation_oracle_subclasses_inherit_its_baseline_backed_attribution():
    """Inheritance here is intentional, not accidental.

    ``MitigationOracle`` backs 68 problems and can attribute a deleted
    Deployment because it snapshots one before injection. Its subclasses run
    those same generic checks via ``super().evaluate()``, so they must keep that
    view -- if this regressed, a deleted Deployment would silently become
    ambiguous for a large slice of the suite.

    Written after this test caught exactly that: the subclasses which declare a
    table for one local reason were shadowing the parent's table entirely, so
    ``FDMitigationOracle`` and friends had already lost the override. Hence
    ``Oracle._failure_classes`` merging across the MRO rather than a plain
    attribute lookup.
    """
    from sregym.conductor.oracles.cpu_throttling_mitigation import CpuThrottlingMitigationOracle
    from sregym.conductor.oracles.fd_exhaustion import FDMitigationOracle
    from sregym.conductor.oracles.kafka_producer_leak_mitigation import KafkaProducerLeakOracle
    from sregym.conductor.oracles.nightly_rebalance_oom_mitigation import NightlyRebalanceOOMMitigationOracle

    # The first declares no table of its own; the rest all do, which is the
    # case that was broken.
    for cls in (
        CpuThrottlingMitigationOracle,
        FDMitigationOracle,
        KafkaProducerLeakOracle,
        NightlyRebalanceOOMMitigationOracle,
    ):
        assert cls.fail("required_deployment_missing")["failure_class"] == FailureClass.AGENT_ERROR, (
            f"{cls.__name__} lost MitigationOracle's attribution of a deleted Deployment"
        )
        # Its own local reasons still resolve.
        assert set(cls._failure_classes()) >= set(vars(cls).get("FAILURE_CLASSES", {}))


# ----------------------------------------------------------------------------
# fail_from_exception
# ----------------------------------------------------------------------------


class _Bare(Oracle):
    def evaluate(self, solution=None, trace=None, duration=None) -> dict:
        return {"success": True}


def test_an_api_exception_is_environmental():
    """The distinction the broad ``except Exception`` handlers used to lose.

    Twelve oracles wrapped their whole evaluation and returned a bare failure,
    so the API server refusing to answer scored the same as a bug in the oracle.
    """
    verdict = _Bare.fail_from_exception(ApiException(status=503, reason="Service Unavailable"))

    assert verdict["reason"] == "kubernetes_api_error"
    assert verdict["failure_class"] == FailureClass.ENVIRONMENT_ERROR
    assert verdict["detail"]["status"] == 503


def test_any_other_exception_is_ours():
    verdict = _Bare.fail_from_exception(AttributeError("'NoneType' has no attribute 'spec'"))

    assert verdict["reason"] == "oracle_raised"
    assert verdict["failure_class"] == FailureClass.HARNESS_ERROR
    assert "AttributeError" in verdict["detail"]["error"]


def test_extra_detail_survives():
    verdict = _Bare.fail_from_exception(RuntimeError("boom"), service="frontend")
    assert verdict["detail"]["service"] == "frontend"


# ----------------------------------------------------------------------------
# pods_unready
# ----------------------------------------------------------------------------


class _State:
    def __init__(self, waiting=None, terminated=None):
        self.waiting = waiting
        self.terminated = terminated


class _Reason:
    def __init__(self, reason):
        self.reason = reason


class _Container:
    def __init__(self, name, ready=True, state=None):
        self.name = name
        self.ready = ready
        self.state = state or _State()


class _Pod:
    def __init__(self, name, phase="Running", containers=()):
        self.metadata = type("M", (), {"name": name})()
        self.status = type("S", (), {"phase": phase, "container_statuses": list(containers)})()


@pytest.fixture
def oracle():
    return _Bare.__new__(_Bare)


def test_all_healthy_pods_yield_no_verdict(oracle):
    pods = [_Pod("frontend-1", containers=[_Container("frontend")])]
    assert oracle.pods_unready(pods) is None


def test_a_completed_init_container_is_not_a_failure(oracle):
    """``Completed`` is the normal terminal state for an init container."""
    pods = [_Pod("frontend-1", containers=[_Container("init", state=_State(terminated=_Reason("Completed")))])]
    assert oracle.pods_unready(pods) is None


@pytest.mark.parametrize(
    ("pod", "expected_detail"),
    [
        (_Pod("search-1", phase="Pending"), {"phase": "Pending"}),
        (
            _Pod("search-1", containers=[_Container("search", state=_State(waiting=_Reason("CrashLoopBackOff")))]),
            {"waiting": "CrashLoopBackOff"},
        ),
        (
            _Pod("search-1", containers=[_Container("search", state=_State(terminated=_Reason("OOMKilled")))]),
            {"terminated": "OOMKilled"},
        ),
        (_Pod("search-1", containers=[_Container("search", ready=False)]), {}),
    ],
)
def test_every_unhealthy_shape_is_ambiguous_and_names_the_pod(oracle, pod, expected_detail):
    """One reason, many shapes.

    All four are ``pods_not_ready``: a pod that will not run may be the agent's
    collateral damage or a node that cannot host it, and nothing here separates
    those. What differs is the ``detail``, which is where the shape is recorded.
    """
    verdict = oracle.pods_unready([pod])

    assert verdict["reason"] == "pods_not_ready"
    assert verdict["failure_class"] == FailureClass.AMBIGUOUS
    assert verdict["detail"]["pod"] == "search-1"
    for key, value in expected_detail.items():
        assert verdict["detail"][key] == value


def test_the_first_unhealthy_pod_wins(oracle):
    pods = [
        _Pod("frontend-1", containers=[_Container("frontend")]),
        _Pod("search-1", phase="Failed"),
        _Pod("rate-1", phase="Pending"),
    ]
    assert oracle.pods_unready(pods)["detail"]["pod"] == "search-1"


def test_caller_detail_is_merged(oracle):
    verdict = oracle.pods_unready([_Pod("search-1", phase="Pending")], namespace="hotel-reservation")
    assert verdict["detail"]["namespace"] == "hotel-reservation"


def test_pods_with_no_container_statuses_do_not_crash(oracle):
    """``container_statuses`` is None on a pod that has not been scheduled."""
    pod = _Pod("search-1")
    pod.status.container_statuses = None
    assert oracle.pods_unready([pod]) is None


# ----------------------------------------------------------------------------
# The shared helpers are actually reachable from the oracles that need them
# ----------------------------------------------------------------------------


def test_fail_is_available_on_every_oracle():
    for name, cls in ((c.__name__, c) for c in Oracle.__subclasses__()):
        assert callable(getattr(cls, "fail", None)), f"{name} lost access to fail()"


def test_base_helpers_are_classmethods_so_subclass_tables_resolve():
    """``fail`` must see ``cls.FAILURE_CLASSES``, not the base's empty dict.

    The obvious staticmethod spelling silently classifies every oracle-specific
    reason as ambiguous, which is a failure that no individual oracle's tests
    would catch.
    """
    for name in ("fail", "fail_from_exception"):
        assert isinstance(inspect.getattr_static(Oracle, name), classmethod), f"Oracle.{name} must be a classmethod"
