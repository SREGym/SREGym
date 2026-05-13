"""Database platform limit exceeded — PostgreSQL role CONNECTION LIMIT set to 0.

Derived from the `database-platform-limit-exceeded` template mined from public
postmortems. Classic production bug: an operator tightens a platform-level
quota (connection cap, file-descriptor limit, thread pool size) intending to
conserve resources, but the new ceiling is below real traffic demand. Dependent
services fail as soon as they try to acquire a slot.

Here the dial is PostgreSQL's per-role `CONNECTION LIMIT`. Setting `otelu`'s
limit to 0 rejects every new connection from the three services that use it —
accounting, product-catalog, product-reviews — with `FATAL: too many
connections for role "otelu"`.
"""

from sregym.conductor.oracles.behavioral_probes import PostgresConnectOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class DBConnectionLimitExhausted(Problem):
    def __init__(
        self,
        app_name: str = "astronomy_shop",
        role: str = "otelu",
        wrong_limit: int = 0,
    ):
        self.app_name = app_name
        if self.app_name != "astronomy_shop":
            raise ValueError(
                f"DBConnectionLimitExhausted only supports astronomy_shop, got {app_name}"
            )

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.pg_pod = "deploy/postgresql"
        self.pg_superuser = "root"
        self.pg_db = "otel"
        self.role = role
        # PostgresConnectOracle reads these names directly:
        self.pg_role = role
        self.pg_password = "otelp"
        self.pg_host = "postgresql"
        self.wrong_limit = wrong_limit
        self.correct_limit = -1  # PostgreSQL default, i.e. unlimited
        self.faulty_service = "product-catalog"  # canonical user-visible symptom

        self.root_cause = self.build_structured_root_cause(
            component=f"role/{self.role}@postgresql",
            namespace=self.namespace,
            description=(
                f"The PostgreSQL role '{self.role}' has its CONNECTION LIMIT set to "
                f"{self.wrong_limit}, so every service that logs in as this role "
                "(accounting, product-catalog, product-reviews) is rejected by the DB "
                f"with 'FATAL: too many connections for role \"{self.role}\"'. Symptoms "
                "include product-catalog and product-reviews returning errors on every "
                "request and accounting failing to consume its Kafka backlog. The fix "
                f"is to restore the role's connection limit to the default unlimited "
                f"(-1) via ALTER ROLE {self.role} CONNECTION LIMIT -1."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = PostgresConnectOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_role_connection_limit(
            pg_pod=self.pg_pod,
            pg_superuser=self.pg_superuser,
            pg_db=self.pg_db,
            role=self.role,
            limit=self.wrong_limit,
        )
        print(f"Role: {self.role} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_role_connection_limit(
            pg_pod=self.pg_pod,
            pg_superuser=self.pg_superuser,
            pg_db=self.pg_db,
            role=self.role,
            limit=self.correct_limit,
        )
        print(f"Role: {self.role} | Namespace: {self.namespace}")
