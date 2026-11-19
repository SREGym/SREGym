"""A problem whose environment is not the test cluster runs with no kubeconfig.

tests/conftest.py blocks kubeconfig loading and Kubernetes calls, so any cluster
client built on this path fails the test.
"""

import asyncio
from types import SimpleNamespace

import pytest

import sregym.conductor.conductor as conductor_module
from sregym.conductor import conductor_api
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.registry import ProblemRegistry
from sregym.utils.decorators import mark_fault_injected


class _App:
    name = "no-cluster-app"
    namespace = None

    def __init__(self, log):
        self.log = log

    def deploy(self):
        self.log.append("deploy")

    def cleanup(self):
        self.log.append("cleanup")


class _MitigationOracle:
    def __init__(self, log):
        self.log = log

    def capture_baseline(self):
        pass

    def evaluate(self):
        self.log.append("evaluate")
        return {"success": True}


class _NoClusterProblem(Problem):
    uses_cluster = False
    log: list[str] = []

    def __init__(self):
        super().__init__(_App(self.log))
        self.mitigation_oracle = _MitigationOracle(self.log)

    @mark_fault_injected
    def inject_fault(self):
        self.log.append("inject")

    @mark_fault_injected
    def recover_fault(self):
        self.log.append("recover")


@pytest.fixture
def log(monkeypatch):
    entries: list[str] = []
    monkeypatch.setattr(_NoClusterProblem, "log", entries)
    # kubectl, helm and docker are still required on the machine.
    monkeypatch.setattr(conductor_module.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    return entries


def test_conductor_runs_a_problem_without_the_cluster(log):
    conductor = Conductor(ConductorConfig(stages=("mitigation",), uses_cluster=False))
    for name in ("kubectl", "mcp_server", "cluster_state", "cluster_egress", "k8s_proxy"):
        assert not hasattr(conductor, name)
    conductor.problems.PROBLEM_REGISTRY["no_cluster"] = _NoClusterProblem
    conductor.problem_id = "no_cluster"

    conductor.start_k8s_proxy()
    assert conductor.get_agent_kubeconfig_path() is None

    async def run():
        await conductor.start_problem()
        assert conductor.submission_stage == "mitigation"
        await conductor.submit("restored", expected_stage="mitigation")
        await conductor.wait_for_submission_work(timeout=5)

    asyncio.run(run())
    conductor.stop_k8s_proxy()
    conductor.clear_cluster_egress_boundary()

    # Leftover cleanup, the application's deploy, the stage, then its own teardown.
    assert log == ["cleanup", "deploy", "inject", "evaluate", "recover", "cleanup"]
    assert conductor.submission_stage == "done"
    assert conductor.results["Mitigation"] == {"success": True}


def test_registry_lists_and_builds_such_a_problem_without_a_kubeconfig(log):
    registry = ProblemRegistry()
    registry.PROBLEM_REGISTRY["no_cluster"] = _NoClusterProblem

    assert not registry.uses_cluster("no_cluster")
    assert isinstance(registry.get_problem_instance("no_cluster"), _NoClusterProblem)
    # Factory entries (lambdas, partials) carry no attribute and use the cluster.
    factories = [problem_id for problem_id, entry in registry.PROBLEM_REGISTRY.items() if not isinstance(entry, type)]
    assert factories and all(map(registry.uses_cluster, factories))
    assert "kubectl" not in vars(registry)


@pytest.mark.parametrize("uses_cluster", [True, False])
def test_get_app_tells_agents_whether_the_problem_uses_the_cluster(monkeypatch, uses_cluster):
    app = SimpleNamespace(app_name="app", namespace="ns", description="text")
    conductor = SimpleNamespace(app=app, problem=SimpleNamespace(uses_cluster=uses_cluster))
    monkeypatch.setattr(conductor_api, "_conductor", conductor)

    assert asyncio.run(conductor_api.get_app())["uses_cluster"] is uses_cluster
