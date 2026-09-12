"""Fault-state updates must not conceal strict recovery failures."""

from types import SimpleNamespace

import pytest

from sregym.utils.decorators import mark_fault_injected


@pytest.mark.parametrize("strict", [False, True])
def test_successful_recovery_clears_fault_state(strict):
    @mark_fault_injected(strict=strict)
    def recover_fault(self):
        return "recovered"

    problem = SimpleNamespace(fault_injected=True)
    assert recover_fault(problem) == "recovered"
    assert problem.fault_injected is False


def test_strict_recovery_preserves_failure_and_fault_state():
    @mark_fault_injected(strict=True)
    def recover_fault(self):
        raise RuntimeError("recovery failed")

    problem = SimpleNamespace(fault_injected=True)
    with pytest.raises(RuntimeError, match="recovery failed"):
        recover_fault(problem)
    assert problem.fault_injected is True


def test_default_recovery_keeps_existing_behavior():
    @mark_fault_injected
    def recover_fault(self):
        raise RuntimeError("recovery failed")

    problem = SimpleNamespace(fault_injected=True)
    assert recover_fault(problem) is None
    assert problem.fault_injected is False


def test_injection_failure_preserves_failure_and_fault_state():
    @mark_fault_injected
    def inject_fault(self):
        raise RuntimeError("injection failed")

    problem = SimpleNamespace(fault_injected=False)
    with pytest.raises(RuntimeError, match="injection failed"):
        inject_fault(problem)
    assert problem.fault_injected is False


def test_successful_injection_sets_fault_state():
    @mark_fault_injected
    def inject_fault(self):
        return "injected"

    problem = SimpleNamespace(fault_injected=False)
    assert inject_fault(problem) == "injected"
    assert problem.fault_injected is True


def test_stale_hostaliases_recovery_does_not_hide_errors():
    from sregym.conductor.problems.stale_hostaliases_dns_poisoning_astronomy_shop import (
        StaleHostAliasesDNSPoisoningAstronomyShop,
    )

    problem = object.__new__(StaleHostAliasesDNSPoisoningAstronomyShop)
    problem.fault_injected = True

    def failed_stop():
        raise RuntimeError("traffic did not stop")

    problem.stop_traffic = failed_stop
    with pytest.raises(RuntimeError, match="traffic did not stop"):
        problem.recover_fault()
    assert problem.fault_injected is True
