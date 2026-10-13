"""Missing-index sequential-scan degradation on Astronomy Shop's product-catalog."""

import logging
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
    upper = output.upper()
    return "ERROR:" in upper or "FATAL:" in upper


def _first_int(output: str) -> int | None:
    for line in output.strip().splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


class ProductCatalogMissingIndexAstronomyShop(Problem):
    """Drop the index behind product-catalog's ``GetProduct`` read, forcing Seq Scans."""

    POSTGRES_DEPLOY = "postgresql"

    FAULT_MEMORY_LIMIT = "512Mi"

    TARGET_ROWS = 200_000

    BATCH_ROWS = 20_000

    SEED_PREFIX = "PROD"

    PLAN_SIGNIFICANT_ROWS = 1_000

    _READY_TIMEOUT_SECONDS = 300
    _READY_POLL_SECONDS = 5
    _READY_CONSECUTIVE_OK = 3

    _PSQL_TIMEOUT_SECONDS = 180
    _PROBE_TIMEOUT_SECONDS = 30

    _DDL_LOCK_TIMEOUT = "5s"
    _DDL_ATTEMPTS = 6

    def __init__(self):
        super().__init__(app=AstronomyShop())

        self.kubectl = KubeCtl()
        self.problem_id = "product_catalog_missing_index_astronomy_shop"
        self.faulty_service = ["product-catalog"]

        self.schema = "catalog"
        self.table = "products"
        self.id_column = "id"
        self._qualified = f"{self.schema}.{self.table}"

        self.root_cause = self.build_structured_root_cause(
            component=f"postgresql (table {self._qualified})",
            namespace=self.namespace,
            description=(
                "The primary-key index on catalog.products(id) is missing, so product-catalog's "
                "GetProduct lookups (SELECT ... FROM catalog.products WHERE id = $1) fall back to "
                "sequential scans that read the entire catalog on every product-detail view, "
                "instead of index scans. Latency for product-catalog and the services that call it "
                "(frontend, checkout, recommendation, product-reviews) degrades and PostgreSQL "
                "burns CPU scanning the table. Every pod, product-catalog and PostgreSQL included, "
                "stays Running and healthy, and the queries still return correct results, so the "
                "regression is visible only from inside the database (EXPLAIN shows a Seq Scan on "
                "catalog.products; pg_stat_user_tables shows seq_scan and seq_tup_read climbing "
                "while idx_scan stays flat). The fix is to recreate the index on "
                "catalog.products(id), e.g. ALTER TABLE catalog.products ADD PRIMARY KEY (id)."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ProductCatalogMissingIndexMitigationOracle(problem=self)
        self.app.create_workload()

    def _psql(self, sql: str, tuples_only: bool = False, timeout: float | None = None) -> str:
        flags = "-tAf -" if tuples_only else "-f -"
        script = (
            f'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 {flags}'
        )
        cmd = f"kubectl exec -i -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- sh -c '{script}'"
        try:
            return self.kubectl.exec_command_checked(
                cmd, input_data=sql, timeout=self._PSQL_TIMEOUT_SECONDS if timeout is None else timeout
            )
        except RuntimeError as exc:
            text = str(exc)
            return text if _sql_failed(text) else f"ERROR: {text}"

    def _terminate_lock_holders(self) -> None:
        self._psql(
            "SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
            "JOIN pg_class c ON c.oid = l.relation "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{self.schema}' AND c.relname = '{self.table}' "
            "AND l.granted AND l.pid <> pg_backend_pid();",
            timeout=self._PROBE_TIMEOUT_SECONDS,
        )

    def _run_ddl(self, sql: str, description: str) -> None:
        for attempt in range(1, self._DDL_ATTEMPTS + 1):
            out = self._psql(f"SET lock_timeout = '{self._DDL_LOCK_TIMEOUT}'; {sql}")
            if not _sql_failed(out):
                return
            lowered = out.lower()
            if "lock timeout" not in lowered and "canceling statement due to lock" not in lowered:
                raise RuntimeError(f"{description} failed: {out.strip()}")
            logger.warning(
                "[missing-index] %s is blocked on a table lock (attempt %d/%d); terminating lock holders",
                description,
                attempt,
                self._DDL_ATTEMPTS,
            )
            self._terminate_lock_holders()
        raise RuntimeError(
            f"{description}: could not acquire a lock on {self._qualified} after {self._DDL_ATTEMPTS} attempts"
        )

    def _wait_for_catalog_ready(self) -> None:
        deadline = time.monotonic() + self._READY_TIMEOUT_SECONDS
        consecutive = 0
        while time.monotonic() < deadline:
            rows = _first_int(
                self._psql(
                    f"SELECT count(*) FROM {self._qualified};",
                    tuples_only=True,
                    timeout=self._PROBE_TIMEOUT_SECONDS,
                )
            )
            consecutive = consecutive + 1 if rows else 0
            if consecutive >= self._READY_CONSECUTIVE_OK:
                return
            time.sleep(self._READY_POLL_SECONDS)
        raise RuntimeError(f"{self._qualified} did not become queryable within {self._READY_TIMEOUT_SECONDS}s")

    def _analyze(self) -> None:
        self._run_ddl(f"ANALYZE {self._qualified};", "refreshing planner statistics")

    def _set_postgres_memory(self, memory: str) -> None:
        patch = (
            f"kubectl -n {self.namespace} patch deploy {self.POSTGRES_DEPLOY} --type=json "
            f'-p=\'[{{"op":"replace",'
            f'"path":"/spec/template/spec/containers/0/resources/limits/memory",'
            f'"value":"{memory}"}}]\''
        )
        self.kubectl.exec_command_checked(patch, timeout=30)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deploy/{self.POSTGRES_DEPLOY} -n {self.namespace} --timeout=180s",
            timeout=200,
        )

    def catalog_row_count(self) -> int:
        return _first_int(self._psql(f"SELECT count(*) FROM {self._qualified};", tuples_only=True)) or 0

    def _seed_row_count(self) -> int:
        out = self._psql(
            f"SELECT count(*) FROM {self._qualified} WHERE {self.id_column} LIKE '{self.SEED_PREFIX}%';",
            tuples_only=True,
        )
        return _first_int(out) or 0

    def _pristine_row_count(self) -> int:
        out = self._psql(
            f"SELECT count(*) FROM {self._qualified} WHERE {self.id_column} NOT LIKE '{self.SEED_PREFIX}%';",
            tuples_only=True,
        )
        return _first_int(out) or 0

    def _delete_seed_rows(self) -> None:
        self._psql(f"DELETE FROM {self._qualified} WHERE {self.id_column} LIKE '{self.SEED_PREFIX}%';")

    def _column_names(self) -> list[str]:
        out = self._psql(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{self.schema}' AND table_name = '{self.table}' "
            "ORDER BY ordinal_position;",
            tuples_only=True,
        )
        return [line.strip() for line in out.strip().splitlines() if line.strip() and not _sql_failed(line)]

    def _seed_batch(self, columns: list[str], start: int, end: int) -> None:
        others = [c for c in columns if c != self.id_column]
        select_list = ", ".join(f"t.{c}" for c in others)
        insert_list = ", ".join([self.id_column, *others])
        sql = (
            f"INSERT INTO {self._qualified} ({insert_list}) "
            f"SELECT '{self.SEED_PREFIX}' || lpad(g::text, 7, '0'), {select_list} "
            f"FROM generate_series({start}, {end}) g "
            f"JOIN (SELECT p.*, (row_number() OVER (ORDER BY {self.id_column})) - 1 AS _rn, "
            f"count(*) OVER () AS _n FROM {self._qualified} p "
            f"WHERE p.{self.id_column} NOT LIKE '{self.SEED_PREFIX}%') t "
            f"ON t._rn = g % t._n;"
        )
        out = self._psql(sql)
        if _sql_failed(out):
            raise RuntimeError(f"Failed to seed catalog rows [{start}, {end}]: {out.strip()}")

    def _seed_to(self, target: int) -> None:
        columns = self._column_names()
        if not columns:
            raise RuntimeError(f"Could not read the column list for {self._qualified}")
        if self.id_column not in columns:
            raise RuntimeError(f"{self._qualified} has no '{self.id_column}' column; the catalog schema changed")

        start = self._seed_row_count() + 1
        while start <= target:
            end = min(start + self.BATCH_ROWS - 1, target)
            self._seed_batch(columns, start, end)
            logger.info("[missing-index] seeded catalog rows %d-%d of %d", start, end, target)
            start = end + 1

    def id_index_exists(self) -> bool:
        out = self._psql(
            "SELECT count(*) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
            f"WHERE n.nspname = '{self.schema}' AND c.relname = '{self.table}' "
            f"AND a.attname = '{self.id_column}';",
            tuples_only=True,
        )
        return (_first_int(out) or 0) > 0

    def _pk_constraint_name(self) -> str | None:
        out = self._psql(
            f"SELECT conname FROM pg_constraint WHERE conrelid = '{self._qualified}'::regclass AND contype = 'p';",
            tuples_only=True,
        )
        for line in out.strip().splitlines():
            line = line.strip()
            if line and not _sql_failed(line):
                return line
        return None

    def _drop_id_index(self) -> None:
        pk = self._pk_constraint_name()
        if pk:
            self._run_ddl(
                f'ALTER TABLE {self._qualified} DROP CONSTRAINT "{pk}";',
                f"dropping primary key {pk!r}",
            )
            return

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
            self._run_ddl(f"DROP INDEX {idx};", f"dropping index {idx!r}")

    def _recreate_id_index(self) -> None:
        if self.id_index_exists():
            return
        try:
            self._run_ddl(
                f"ALTER TABLE {self._qualified} ADD PRIMARY KEY ({self.id_column});",
                "adding the primary key",
            )
        except RuntimeError as exc:
            logger.warning("[missing-index] %s; falling back to a plain index", exc)
            self._run_ddl(
                f"CREATE INDEX IF NOT EXISTS {self.table}_{self.id_column}_idx "
                f"ON {self._qualified} ({self.id_column});",
                "creating the id index",
            )

    def explain_getproduct_plan(self) -> str:
        return self._psql(f"EXPLAIN SELECT * FROM {self._qualified} p WHERE p.{self.id_column} = 'OLJCESPC7Z';")

    @staticmethod
    def plan_uses_index_scan(plan: str) -> bool:
        lowered = plan.lower()
        return ("index scan" in lowered or "index only scan" in lowered) and "seq scan" not in lowered

    def product_catalog_deployment_present(self) -> bool:
        try:
            deployment = self.kubectl.get_deployment(self.faulty_service[0], self.namespace)
        except Exception:  # noqa: BLE001 - a missing deployment is a failed guard, not a crash
            return False
        if deployment is None:
            return False
        replicas = deployment.spec.replicas
        return replicas is None or replicas > 0

    @mark_fault_injected
    def inject_fault(self):
        logger.info("Injecting product-catalog missing-index fault...")

        self._set_postgres_memory(self.FAULT_MEMORY_LIMIT)
        self._wait_for_catalog_ready()

        self.kubectl.exec_command(f"kubectl rollout status deploy/product-catalog -n {self.namespace} --timeout=120s")

        self._delete_seed_rows()
        self._recreate_id_index()
        if self._pristine_row_count() < 1:
            raise RuntimeError(f"{self._qualified} has no template row to clone; cannot seed the catalog.")

        self._seed_to(self.TARGET_ROWS)
        self._analyze()

        plan = self.explain_getproduct_plan()
        if not self.plan_uses_index_scan(plan):
            raise RuntimeError(
                f"Planner did not choose an Index Scan at {self.TARGET_ROWS} rows, so there is no "
                f"index-vs-seqscan signal to create here. Plan was:\n{plan}"
            )

        self._drop_id_index()
        self._analyze()
        plan = self.explain_getproduct_plan()
        if "seq scan" not in plan.lower():
            raise RuntimeError(f"Expected a Seq Scan after dropping the index, but the plan was:\n{plan}")

        logger.info("product-catalog missing-index fault injected (%d rows, Seq Scan confirmed).", self.TARGET_ROWS)
        return True

    @mark_fault_injected
    def recover_fault(self):
        logger.info("Recovering from product-catalog missing-index fault...")
        self._wait_for_catalog_ready()
        self._recreate_id_index()
        self._analyze()

        plan = self.explain_getproduct_plan()
        if not self.plan_uses_index_scan(plan) and self.catalog_row_count() >= self.PLAN_SIGNIFICANT_ROWS:
            raise RuntimeError(f"Index was restored but the plan is still not an Index Scan:\n{plan}")

        logger.info("product-catalog missing-index fault recovered.")
        return True
