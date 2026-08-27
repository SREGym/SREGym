from types import SimpleNamespace

from sregym.conductor.problems.base import Problem
from sregym.service.khaos_capabilities import KhaosCapability
from sregym.utils.decorators import mark_fault_injected


class ExampleProblem(Problem):
    @mark_fault_injected
    def inject_fault(self):
        pass

    @mark_fault_injected
    def recover_fault(self):
        pass


def test_namespace_defaults_to_application_namespace():
    app = SimpleNamespace(namespace="application-namespace")

    problem = ExampleProblem(app=app)

    assert problem.app is app
    assert problem.namespace == "application-namespace"


def test_explicit_namespace_overrides_application_namespace():
    app = SimpleNamespace(namespace="application-namespace")

    problem = ExampleProblem(app=app, namespace="custom-namespace")

    assert problem.namespace == "custom-namespace"


def test_explicit_empty_namespace_is_preserved():
    app = SimpleNamespace(namespace="application-namespace")

    problem = ExampleProblem(app=app, namespace="")

    assert problem.namespace == ""


def test_problem_without_khaos_has_no_host_capabilities():
    app = SimpleNamespace(namespace="application-namespace")

    problem = ExampleProblem(app=app)

    assert problem.khaos_capabilities() == frozenset()


def test_legacy_khaos_problem_defaults_to_ebpf_syscall_capability():
    class LegacyKhaosProblem(ExampleProblem):
        def requires_khaos(self) -> bool:
            return True

    app = SimpleNamespace(namespace="application-namespace")

    problem = LegacyKhaosProblem(app=app)

    assert problem.khaos_capabilities() == frozenset({KhaosCapability.EBPF_SYSCALL})
