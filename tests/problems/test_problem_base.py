from types import SimpleNamespace

from sregym.conductor.problems.base import Problem


class ExampleProblem(Problem):
    def inject_fault(self):
        pass

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
