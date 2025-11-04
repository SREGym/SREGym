from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.cockroachdb_app import CockroachDBApplication
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class ScalePodCockroachDB(Problem):

    def __init__(self):
        self.app = CockroachDBApplication()
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace

        super().__init__(app=self.app, namespace=self.app.namespace)
        self.mitigation_oracle = MitigationOracle(problem=self)

    @property
    def description(self) -> str:
        """Description of the problem."""
        return "Scale CockroachDB cluster up to have 2 more replicas."

    @mark_fault_injected
    def inject_fault(self):
        pass

    @mark_fault_injected
    def recover_fault(self):
        pass
