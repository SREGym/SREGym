"""Unit tests for the CPU-throttling brownout fault (cluster-free)."""

from sregym.conductor.problems.base import Problem


def test_root_cause_is_structured():
    rc = Problem.build_structured_root_cause(
        component="deployment/frontend",
        namespace="hotel-reservation",
        description="CFS throttling from a too-tight cpu limit.",
    )
    assert rc.startswith("[fault_spec]")
    assert "component=deployment/frontend" in rc
    assert "namespace=hotel-reservation" in rc
    assert "||" in rc