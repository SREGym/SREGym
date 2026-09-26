"""Integer overflow on a primary-key sequence.

Derived from the `integer-overflow-primary-key` template mined from public
postmortems. Classic production bug class: a table's PK is declared as INT4
(2^31 - 1 = 2,147,483,647 max). Under sufficient write volume the sequence
eventually exhausts and inserts start failing with `ERROR: integer out of
range`. The fix is either to widen the column to BIGINT (the real fix, a
schema migration / code change) or — as a short-term band-aid — to reset the
sequence if there's slack in the used-id space.

In astronomy-shop, `reviews.productreviews.id` is an INTEGER identity column.
Seeded at 50 after init; we set the sequence to one below INT_MAX so the very
next insert hits the overflow.
"""

from sregym.conductor.oracles.behavioral_probes import ProductReviewsInsertOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


_INT4_MAX = 2147483647


class IntegerOverflowPrimaryKey(Problem):
    def __init__(self, app_name: str = "astronomy_shop"):
        self.app_name = app_name
        if self.app_name != "astronomy_shop":
            raise ValueError(
                f"IntegerOverflowPrimaryKey only supports astronomy_shop, got {app_name}"
            )

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.faulty_service = "product-reviews"  # the writer we'd expect to hit it
        self.pg_pod = "deploy/postgresql"
        self.pg_superuser = "root"
        self.pg_db = "otel"
        self.column_schema = "reviews"
        self.column_table = "productreviews"
        self.column_name = "id"
        self.sequence = "reviews.productreviews_id_seq"
        self.wrong_value = _INT4_MAX - 1  # next insert will try _INT4_MAX, still fits;
        # but setval sets "last_value" so nextval returns that+1 = overflow.
        self.safe_value = 1000
        self.min_headroom = _INT4_MAX // 2  # mitigation succeeds if >= 1B headroom or bigint

        self.root_cause = self.build_structured_root_cause(
            component=f"table/{self.column_schema}.{self.column_table}@postgresql",
            namespace=self.namespace,
            description=(
                f"The identity sequence for {self.column_schema}.{self.column_table}."
                f"{self.column_name} is pinned at {self.wrong_value} — effectively "
                "INT4 max. The next insert calls nextval() which overflows and "
                "postgres returns `ERROR: nextval: reached maximum value of "
                "sequence ... (2147483647)` (or `integer out of range` depending on "
                "how the app constructs the insert). The column type is still "
                "`integer`, so the real fix is a schema migration to `ALTER COLUMN "
                f"{self.column_name} TYPE bigint`. A short-term band-aid is to "
                f"reset the sequence via `SELECT setval(...)` if the id-space has "
                "slack, but that only buys time."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = ProductReviewsInsertOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.set_sequence_value(
            pg_pod=self.pg_pod,
            pg_superuser=self.pg_superuser,
            pg_db=self.pg_db,
            sequence=self.sequence,
            value=self.wrong_value,
        )
        print(f"Sequence: {self.sequence} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.set_sequence_value(
            pg_pod=self.pg_pod,
            pg_superuser=self.pg_superuser,
            pg_db=self.pg_db,
            sequence=self.sequence,
            value=self.safe_value,
        )
        print(f"Sequence: {self.sequence} | Namespace: {self.namespace}")
