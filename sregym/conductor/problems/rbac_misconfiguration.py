from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class RBACMisconfiguration(Problem):
    def __init__(self, app_name: str = "astronomy_shop", faulty_service: str = "frontend"):
        if app_name == "astronomy_shop":
            self.app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")

        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The ServiceAccount and RBAC role bindings remove required ConfigMap read permissions while an init "
                "container still depends on ConfigMap access, causing init failures and keeping pods stuck before Ready. "
                "Symptoms include Forbidden authorization errors in init logs and pods remaining in Init or CrashLoop phases."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

        # self.mitigation_oracle = CompoundedOracle(self, WorkloadOracle(problem=self, wrk_manager=self.app.wrk))

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: RBAC Init Container Misconfiguration ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(fault_type="rbac_misconfiguration", microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: RBAC Init Container Misconfiguration ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(fault_type="rbac_misconfiguration", microservices=[self.faulty_service])
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
