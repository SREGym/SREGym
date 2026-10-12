from collections.abc import Callable

from sregym.conductor.oracles.apps.gitea.application import GiteaApplicationOracle
from sregym.conductor.oracles.deathstarbench import ScaledServiceEndpointOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.postgres_application import PostgresApplicationOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.app_registry import AppRegistry, RecoveryChecks
from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

RECOVERY_ORACLES = {
    RecoveryChecks.ENDPOINTS: ServiceEndpointMitigationOracle,
    RecoveryChecks.POSTGRES_RECORDS: PostgresApplicationOracle,
    RecoveryChecks.GIT_REPOSITORIES: GiteaApplicationOracle,
    RecoveryChecks.MONGODB_RECORDS: ScaledServiceEndpointOracle,
}


class WrongServiceSelector(Problem):
    """The ``wrong_service_selector_social_network`` configuration is
    deprecated in SREGym 1.1 after 21/21 successes.
    """

    def __init__(
        self,
        app_name="astronomy_shop",
        faulty_service="frontend",
        scale_tier=None,
        *,
        app_factory: Callable[[], Application] | None = None,
        oracle_factory: Callable | None = None,
        expected_service_port: int | None = None,
    ):
        self.app_name = app_name
        self.faulty_service = faulty_service

        if app_factory is None:
            definition = AppRegistry().get_definition(app_name)
            app = definition.create(scale_tier)
            oracle_factory = oracle_factory or RECOVERY_ORACLES[definition.checks(scale_tier)]
            expected_service_port = expected_service_port or definition.service_port
        else:
            if scale_tier is not None:
                raise ValueError("Configure the scale tier in app_factory instead")
            app = app_factory()
            oracle_factory = oracle_factory or ServiceEndpointMitigationOracle
        self.expected_service_port = expected_service_port or app.frontend_port
        super().__init__(app=app)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The service `{self.faulty_service}` has a misconfigured selector that adds an incorrect label, "
                "so it no longer matches the intended backing pods. The service has zero or insufficient endpoints "
                "despite healthy-looking deployments, causing routing failures at the service layer. Users observe "
                "connection resets/timeouts and partial outages when requests are sent through this service."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = oracle_factory(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="wrong_service_selector",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="wrong_service_selector",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
