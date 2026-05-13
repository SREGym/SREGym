"""Operator error: wrong host in a service's upstream connection string.

Derived from the `operator-error-wrong-host` template mined from public
postmortems. Classic fat-finger bug: an operator editing a config rolls a
one-character change into a hostname, the typo passes CI because unit tests
mock the upstream, and the service degrades in production with DNS-resolution
failures.
"""

from sregym.conductor.oracles.behavioral_probes import AccountingHostResolvableOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


# Per-service configuration:
#   env_var, correct_value, wrong_value
_VARIANTS = {
    "accounting": (
        "DB_CONNECTION_STRING",
        "Host=postgresql;Username=otelu;Password=otelp;Database=otel",
        "Host=postresql;Username=otelu;Password=otelp;Database=otel",
    ),
    "product-reviews": (
        "PRODUCT_CATALOG_ADDR",
        "product-catalog:8080",
        "product-catlog:8080",
    ),
    "recommendation": (
        "PRODUCT_CATALOG_ADDR",
        "product-catalog:8080",
        "product-catlog:8080",
    ),
    "frontend": (
        "CART_ADDR",
        "cart:8080",
        "carrt:8080",
    ),
    "checkout": (
        "PAYMENT_ADDR",
        "payment:8080",
        "paymetn:8080",
    ),
}


class OperatorErrorWrongHost(Problem):
    def __init__(
        self,
        app_name: str = "astronomy_shop",
        faulty_service: str = "accounting",
    ):
        self.app_name = app_name
        self.faulty_service = faulty_service

        if self.app_name != "astronomy_shop":
            raise ValueError(
                f"OperatorErrorWrongHost only supports astronomy_shop, got {app_name}"
            )
        if self.faulty_service not in _VARIANTS:
            raise ValueError(
                f"OperatorErrorWrongHost has no variant for service '{faulty_service}'; "
                f"known variants: {sorted(_VARIANTS)}"
            )

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        env_var, correct_value, wrong_value = _VARIANTS[self.faulty_service]
        self.env_var = env_var
        self.correct_value = correct_value
        self.wrong_value = wrong_value

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.env_var} env var on {self.faulty_service} has a typo "
                f"in the hostname (value '{self.wrong_value}'). The Kubernetes "
                "DNS service can't resolve the wrong name, so every new "
                f"connection from {self.faulty_service} to that upstream fails "
                "with a resolver error (`Name or service not known` / `no such "
                "host`). The fix is to correct the hostname in the deployment's "
                "env."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = AccountingHostResolvableOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_env_value_override(
            deployment_name=self.faulty_service,
            env_var=self.env_var,
            wrong_value=self.wrong_value,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.recover_env_value_override(
            deployment_name=self.faulty_service,
            env_var=self.env_var,
            correct_value=self.correct_value,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
