"""Missing-index sequential-scan degradation on Astronomy Shop's product-catalog.

Real-world pattern: an index backing a hot read is dropped (a migration recreates
the table or forgets to re-add the index). Every ``GetProduct`` lookup
(``SELECT ... FROM catalog.products WHERE id = $1``) then switches from an
Index Scan to a full Sequential Scan, so each lookup reads the whole table
instead of walking the index. Every pod — product-catalog and PostgreSQL alike —
stays ``Running`` and healthy throughout; the regression is visible only from the
query plan.

The fault is a deterministic DDL change (drop the index), and grading is
plan-based (``EXPLAIN`` must show an Index Scan again), so the result does not
depend on machine speed. ``inject_fault`` additionally grows the catalog until
the planner prefers an Index Scan *with* the index present on this particular
machine, then asserts a Seq Scan once the index is dropped — a self-check that
the expected symptom actually manifests here.

Sizing note: product-catalog's ``ListProducts`` runs ``SELECT ... FROM
catalog.products ORDER BY p.id`` with no WHERE and no LIMIT, so it materialises
every row to build its gRPC response. That path cannot benefit from the id index
at any size, and on the Helm chart's stock 20Mi limit a large catalog would
OOMKill the service — turning this into an out-of-memory fault rather than a
missing-index one. The seeded catalog is therefore deliberately small: big enough
that the planner prefers an Index Scan when the index exists, small enough that
product-catalog stays comfortably inside its stock limit. This problem does not
touch product-catalog's resources at all.
"""

import logging
import shlex
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.product_catalog_missing_index_mitigation import (
    ProductCatalogMissingIndexMitigationOracle,
)
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


def _sql_failed(output: str) -> bool:
    """Return True if a psql output string reports a SQL-level error."""
    upper = output.upper()
    return "ERROR:" in upper or "FATAL:" in upper


def _first_int(output: str) -> int:
    """Parse the last non-empty line of psql tuples-only output as an int."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line:
            try:
                return int(line)
            except ValueError:
                return 0
    return 0


class ProductCatalogMissingIndexAstronomyShop(Problem):
    """Drop the index behind product-catalog's ``GetProduct`` read, forcing Seq Scans."""

    # PostgreSQL backend (matches the Astronomy Shop Helm chart).
    _POSTGRES_READY_TIMEOUT_SECONDS = 120
    _POSTGRES_POLL_INTERVAL_SECONDS = 3

    def __init__(self):
        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        # Visibly-degraded service and the database it reads from.
        self.faulty_service = "product-catalog"
        self.backend_service = "postgresql"
        self.db_user = "otelu"
        self.db_name = "otel"
        self.db_password = "otelp"

        # Target table for the hot read.
        self.schema = "catalog"
        self.table = "products"
        self.id_column = "id"
        self._qualified = f"{self.schema}.{self.table}"

        # Synthetic rows carry ordinary-looking SKUs (e.g. ``PROD000001``); the
        # prefix is only used internally to seed and clean them up. Nothing here
        # names SREGym, the fault, or the benchmark, but note the disguise is not
        # airtight: the ids are sequential where the genuine ones are random
        # 10-character strings, so an agent that inspects the catalog closely can
        # still tell which rows were added.
        self.seed_prefix = "PROD"

        # Grow the catalog so the planner reliably prefers an Index Scan when the
        # index exists. ``max_rows`` bounds the adaptive growth on slow machines
        # and is kept well below the row count that would pressure the service's
        # stock 20Mi limit via the un-indexable ListProducts path.
        self.target_rows = 3_000
        self.max_rows = 12_000
        self.batch_size = 5_000

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The primary-key index on catalog.products(id) is missing, so "
                "product-catalog's GetProduct lookups "
                "(SELECT ... FROM catalog.products WHERE id = $1) perform "
                "sequential scans instead of index scans. All pods, including "
                "product-catalog and PostgreSQL, stay Running and healthy; the "
                "regression is only visible from the query plan (Seq Scan on "
                "catalog.products). The fix is to recreate the index on "
                "catalog.products(id)."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ProductCatalogMissingIndexMitigationOracle(problem=self)
        self.app.create_workload()

    # ------------------------------------------------------------------ #
    # PostgreSQL helpers                                                  #
    # ------------------------------------------------------------------ #
    def _psql(self, sql: str, tuples_only: bool = False) -> str:
        """Run a single SQL statement inside the PostgreSQL pod via ``kubectl exec``."""
        psql_args = "-tAc" if tuples_only else "-c"
        script = (
            'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" '
            f"-d {shlex.quote(self.db_name)} -v ON_ERROR_STOP=1 {psql_args} {shlex.quote(sql)}"
        )
        return self.kubectl.exec_command(
            f"kubectl exec -n {self.namespace} deploy/{self.backend_service} -- sh -lc {shlex.quote(script)}"
        )

    def _wait_postgres_ready(self) -> None:
        """Block until PostgreSQL answers a trivial query, or raise on timeout."""
        deadline = time.monotonic() + self._POSTGRES_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            out = self._psql("SELECT 1;", tuples_only=True)
            if _first_int(out) == 1:
                return
            time.sleep(self._POSTGRES_POLL_INTERVAL_SECONDS)
        raise RuntimeError("PostgreSQL did not become ready in time for fault injection.")

    def _analyze(self) -> None:
        """Refresh planner statistics so row estimates reflect the seeded size."""
        self._psql(f"ANALYZE {self._qualified};")

    def _seed_row_count(self) -> int:
        out = self._psql(
            f"SELECT count(*) FROM {self._qualified} WHERE {self.id_column} LIKE '{self.seed_prefix}%';",
            tuples_only=True,
        )
        return _first_int(out)

    def _original_row_count(self) -> int:
        out = self._psql(
            f"SELECT count(*) FROM {self._qualified} WHERE {self.id_column} NOT LIKE '{self.seed_prefix}%';",
            tuples_only=True,
        )
        return _first_int(out)

    def _delete_seed_rows(self) -> None:
        """Remove any previously seeded rows so injection is idempotent."""
        self._psql(f"DELETE FROM {self._qualified} WHERE {self.id_column} LIKE '{self.seed_prefix}%';")

    def _seed_batch(self, start_g: int, end_g: int) -> None:
        """Clone the real catalog rows round-robin with fresh unique ids.

        Uses ``jsonb_populate_record`` so it is schema-agnostic: every column is
        copied from a real template row and only ``id`` is overridden, which
        satisfies NOT NULL / typed columns without hard-coding the schema.

        Templates rotate across *all* genuine rows rather than cloning one of
        them, so the seeded catalog carries the same spread of names, prices and
        descriptions as the real one. Cloning a single row would leave tens of
        thousands of identically-named products — an obvious tell that the
        catalog was inflated on purpose.
        """
        sql = (
            f"INSERT INTO {self._qualified} "
            f"SELECT (jsonb_populate_record(NULL::{self._qualified}, "
            f"to_jsonb(t) || jsonb_build_object("
            f"'{self.id_column}', '{self.seed_prefix}' || lpad(g::text, 6, '0')))).* "
            f"FROM generate_series({start_g}, {end_g}) g "
            f"JOIN (SELECT p.*, row_number() OVER (ORDER BY {self.id_column}) - 1 AS _rn, "
            f"count(*) OVER () AS _n FROM {self._qualified} p "
            f"WHERE p.{self.id_column} NOT LIKE '{self.seed_prefix}%') t "
            f"ON t._rn = g % t._n;"
        )
        out = self._psql(sql)
        if _sql_failed(out):
            raise RuntimeError(f"Failed to seed catalog rows [{start_g}, {end_g}]: {out.strip()}")

    def _seed_to(self, target: int) -> None:
        """Seed rows in batches until at least ``target`` synthetic rows exist."""
        current = self._seed_row_count()
        for start_g in range(current + 1, target + 1, self.batch_size):
            end_g = min(start_g + self.batch_size - 1, target)
            self._seed_batch(start_g, end_g)

    # ------------------------------------------------------------------ #
    # Index / query-plan helpers                                         #
    # ------------------------------------------------------------------ #
    def _id_index_exists(self) -> bool:
        """Return True if any index covers the id column of the target table."""
        out = self._psql(
            "SELECT count(*) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
            f"WHERE n.nspname = '{self.schema}' AND c.relname = '{self.table}' "
            f"AND a.attname = '{self.id_column}';",
            tuples_only=True,
        )
        return _first_int(out) > 0

    def _pk_constraint_name(self) -> str | None:
        out = self._psql(
            f"SELECT conname FROM pg_constraint " f"WHERE conrelid = '{self._qualified}'::regclass AND contype = 'p';",
            tuples_only=True,
        )
        for line in out.strip().splitlines():
            line = line.strip()
            if line and not _sql_failed(line):
                return line
        return None

    def _drop_id_index(self) -> None:
        """Drop the index backing the id read (primary key, or a plain index)."""
        pk = self._pk_constraint_name()
        if pk:
            out = self._psql(f'ALTER TABLE {self._qualified} DROP CONSTRAINT "{pk}";')
            if _sql_failed(out):
                raise RuntimeError(f"Failed to drop primary key {pk!r}: {out.strip()}")
            return
        # No primary key: drop any standalone index covering id.
        idx = self._psql(
            "SELECT indexrelid::regclass::text FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
            f"WHERE n.nspname = '{self.schema}' AND c.relname = '{self.table}' "
            f"AND a.attname = '{self.id_column}' LIMIT 1;",
            tuples_only=True,
        ).strip()
        if idx and not _sql_failed(idx):
            self._psql(f"DROP INDEX {idx};")

    def _recreate_id_index(self) -> None:
        """Restore the id index (primary key, falling back to a plain index)."""
        if self._id_index_exists():
            return
        out = self._psql(f"ALTER TABLE {self._qualified} ADD PRIMARY KEY ({self.id_column});")
        if _sql_failed(out):
            fallback = self._psql(
                f"CREATE INDEX IF NOT EXISTS {self.table}_{self.id_column}_idx "
                f"ON {self._qualified} ({self.id_column});"
            )
            if _sql_failed(fallback):
                raise RuntimeError(f"Failed to recreate id index: {out.strip()} / {fallback.strip()}")

    def explain_getproduct_plan(self) -> str:
        """Return the query plan for the GetProduct-shaped lookup.

        Plain ``EXPLAIN`` (no ANALYZE) reports the planner's chosen node types
        without executing the query, so the id value need not exist.
        """
        return self._psql(
            f"EXPLAIN SELECT * FROM {self._qualified} " f"WHERE {self.id_column} = '{self.seed_prefix}000001';"
        )

    @staticmethod
    def plan_uses_index_scan(plan: str) -> bool:
        """True if the plan uses an index scan and not a sequential scan."""
        lowered = plan.lower()
        return ("index scan" in lowered or "index only scan" in lowered) and "seq scan" not in lowered

    # ------------------------------------------------------------------ #
    # Deployment guard (used by the mitigation oracle)                   #
    # ------------------------------------------------------------------ #
    def product_catalog_deployment_present(self) -> bool:
        """Return whether the product-catalog deployment exists and is not scaled to zero.

        This is an anti-gaming guard only — it stops an agent "fixing" the fault by
        deleting or scaling away the service. It deliberately does *not* check pod
        readiness: readiness is identical with and without the index, so it carries
        no signal, and a transient restart must not be mistaken for a failed fix.
        """
        try:
            deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        except Exception:  # noqa: BLE001 - a missing deployment is a failed guard, not a crash
            return False
        if deployment is None:
            return False
        replicas = deployment.spec.replicas
        return replicas is None or replicas > 0

    # ------------------------------------------------------------------ #
    # Fault lifecycle                                                    #
    # ------------------------------------------------------------------ #
    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._wait_postgres_ready()

        # Start from a clean, indexed baseline (in case a prior run left state).
        self._delete_seed_rows()
        self._recreate_id_index()
        if self._original_row_count() < 1:
            raise RuntimeError(f"{self._qualified} has no template row to clone; cannot seed the catalog.")

        # 1) Grow the catalog enough for the planner to prefer the index, then
        #    refresh stats. product-catalog's resources are deliberately left at
        #    the chart's stock values so memory never becomes part of the story.
        self._seed_to(self.target_rows)
        self._analyze()

        # 2) Machine-adaptive check: ensure the planner prefers an Index Scan WITH
        #    the index present here; grow the table further if it does not.
        rows = self.target_rows
        while not self.plan_uses_index_scan(self.explain_getproduct_plan()) and rows < self.max_rows:
            rows = min(rows * 2, self.max_rows)
            logger.info("[missing-index] Planner not yet index-preferring; growing catalog to %d rows", rows)
            self._seed_to(rows)
            self._analyze()
        if not self.plan_uses_index_scan(self.explain_getproduct_plan()):
            raise RuntimeError(
                f"Planner did not choose an Index Scan even at {rows} rows; cannot establish a "
                "reliable index-vs-seqscan signal on this machine."
            )

        # 3) Drop the index behind the hot read.
        self._drop_id_index()
        self._analyze()

        # 4) Verify the fault manifests: the same lookup now uses a Seq Scan.
        plan = self.explain_getproduct_plan()
        if "seq scan" not in plan.lower():
            raise RuntimeError(f"Expected a Seq Scan after dropping the index, but the plan was:\n{plan}")

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._wait_postgres_ready()
        if not self._id_index_exists():
            self._recreate_id_index()

        self._delete_seed_rows()
        self._analyze()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
