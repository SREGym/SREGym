from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.unit_mismatch_gomemlimit_mitigation import parse_go_memory_limit
from sregym.conductor.problems.unit_mismatch_gomemlimit import (
    UnitMismatchGomemlimitAstronomyShop,
)


def _problem(service="checkout", expected="16MiB", live="16MiB"):
    problem = UnitMismatchGomemlimitAstronomyShop.__new__(UnitMismatchGomemlimitAstronomyShop)
    problem.namespace = "astronomy-shop"
    problem.faulty_service = service
    problem.env_var = "GOMEMLIMIT"
    problem.expected_chart_value = expected
    problem.wrong_value = expected.replace("MiB", "MB")
    problem._baseline_template = None
    problem._baseline_replicas = None
    container = SimpleNamespace(
        name=service,
        env=[SimpleNamespace(name="GOMEMLIMIT", value=live)],
    )
    deployment = SimpleNamespace(
        spec=SimpleNamespace(replicas=1, template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
    )
    problem.kubectl = SimpleNamespace(get_deployment=lambda name, namespace: deployment)
    return problem


def test_rejects_a_service_with_no_variant():
    with pytest.raises(ValueError, match="no variant"):
        UnitMismatchGomemlimitAstronomyShop(faulty_service="frontend")


def test_injected_value_is_one_the_go_runtime_actually_rejects():
    # The whole fault depends on this: if the SI form ever became valid, the
    # problem would silently stop breaking anything.
    for correct in ("16MiB", "60MiB"):
        wrong = correct.replace("MiB", "MB")
        assert parse_go_memory_limit(correct) is not None
        assert parse_go_memory_limit(wrong) is None


def test_configured_limit_reads_the_target_container():
    problem = _problem(live="16MiB")
    deployment = problem.kubectl.get_deployment("checkout", "astronomy-shop")

    assert problem._configured_limit(deployment) == "16MiB"


def test_injection_refuses_when_the_chart_value_has_drifted():
    # The root-cause text names a specific value; if the chart no longer sets
    # it, injecting anyway would hand the judge a description that does not
    # match reality.
    problem = _problem(expected="16MiB", live="32MiB")
    deployment = problem.kubectl.get_deployment("checkout", "astronomy-shop")

    assert problem._configured_limit(deployment) != problem.expected_chart_value


def test_root_cause_localises_to_the_faulty_deployment():
    problem = UnitMismatchGomemlimitAstronomyShop.__new__(UnitMismatchGomemlimitAstronomyShop)
    text = problem.build_structured_root_cause(
        component="deployment/checkout",
        namespace="astronomy-shop",
        description="x",
    )

    assert text.startswith("[fault_spec] component=deployment/checkout; namespace=astronomy-shop")
