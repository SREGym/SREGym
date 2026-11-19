"""A problem can declare host endpoints its environment serves to the agent."""

from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.registry import ProblemRegistry


class _ServingProblem(Problem):
    agent_endpoints = ("http://host.docker.internal:28310", "http://host.docker.internal:28311")


def test_registry_returns_a_problems_declared_endpoints():
    registry = ProblemRegistry()
    registry.PROBLEM_REGISTRY["serving"] = _ServingProblem

    assert registry.agent_endpoints("serving") == _ServingProblem.agent_endpoints


def test_problems_and_factories_serve_no_endpoints_by_default():
    registry = ProblemRegistry()
    served = {"usage_reporting"}  # the problems that declare endpoints
    for problem_id in set(registry.get_problem_ids()) - served:
        assert registry.agent_endpoints(problem_id) == ()
    assert registry.agent_endpoints("not-registered") == ()
