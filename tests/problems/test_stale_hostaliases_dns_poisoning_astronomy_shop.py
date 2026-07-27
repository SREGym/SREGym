"""Pytest test file for stale_hostaliases DNS poisoning fault in Astronomy Shop."""

import sys
from unittest.mock import MagicMock

# Mock out modules that depend on building/downloading litellm or langchain, Unix-only, or SSH-related modules
sys.modules['litellm'] = MagicMock()
sys.modules['openai'] = MagicMock()
sys.modules['langchain_core'] = MagicMock()
sys.modules['langchain_core.messages'] = MagicMock()
sys.modules['langchain_litellm'] = MagicMock()
sys.modules['llm_backend'] = MagicMock()
sys.modules['llm_backend.init_backend'] = MagicMock()
sys.modules['llm_backend.get_llm_backend'] = MagicMock()
sys.modules['sregym.conductor.oracles.llm_as_a_judge.judge'] = MagicMock()
sys.modules['resource'] = MagicMock()
sys.modules['paramiko'] = MagicMock()
sys.modules['paramiko.client'] = MagicMock()

import pytest

from sregym.conductor.problems.stale_hostaliases_dns_poisoning_astronomy_shop import (
    StaleHostAliasesDNSPoisoningAstronomyShop,
)
from sregym.service.kubectl import KubeCtl


@pytest.mark.integration
def test_stale_hostaliases_injection_and_recovery():
    """Test fault injection and recovery for stale_hostaliases DNS poisoning on Astronomy Shop deployment."""
    problem = StaleHostAliasesDNSPoisoningAstronomyShop()
    kubectl = KubeCtl()

    # 1. Inject fault
    problem.inject_fault()

    # Fetch deployment state after fault injection
    deploy = kubectl.apps_v1_api.read_namespaced_deployment(problem.faulty_service, problem.namespace)
    host_aliases = deploy.spec.template.spec.host_aliases

    # Assert hostAliases is injected properly
    assert host_aliases is not None and len(host_aliases) > 0, "host_aliases should not be None or empty after injection"
    assert host_aliases[0].ip == problem.blackhole_ip, f"Expected blackhole IP {problem.blackhole_ip}, got {host_aliases[0].ip}"
    assert problem.target_backend in host_aliases[0].hostnames, f"Expected {problem.target_backend} in hostnames {host_aliases[0].hostnames}"

    # 2. Recover fault
    problem.recover_fault()

    # Fetch deployment state after fault recovery
    deploy = kubectl.apps_v1_api.read_namespaced_deployment(problem.faulty_service, problem.namespace)
    host_aliases = deploy.spec.template.spec.host_aliases

    # Assert hostAliases is removed
    assert host_aliases is None or len(host_aliases) == 0, "host_aliases should be None or empty after recovery"
