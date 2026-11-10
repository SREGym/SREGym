"""Missing-index sequential-scan degradation on Astronomy Shop's product-reviews"""

import csv
import hashlib
import io
import json
import logging
import shlex
import time
from datetime import date, datetime, timedelta

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.product_reviews_missing_index_mitigation import (
    ProductReviewsMissingIndexMitigationOracle,
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


class ProductReviewsMissingIndexAstronomyShop(Problem):
    """
    Drop the index behind product-reviews' lookups on reviews.productreviews, forcing Seq Scans.

    The table keeps the review history of products that are no longer in the catalog
    (about 294k rows). product-reviews looks reviews up by product_id on every request
    (GetProductReviews and GetAverageProductReviewScore, a new connection each time), so
    without product_id_index every lookup is a parallel sequential scan. Every pod stays
    Running and every response is still correct. The cause is only visible inside the
    database. The fix is an index the planner uses for product_id again, without
    deleting the history.

    The database says the history is real rather than leaving it to be guessed: each
    review has a created_at going back years, and catalog.discontinued_products lists every
    retired product with its name and the day it left the catalog. Without these the rows
    look like junk for product ids that don't exist, and deleting them looks like part of
    the fix.

    Both are written into the postgresql-init ConfigMap before Postgres restarts, so the
    live schema matches init.sql. Added to the live database only, an agent that diffs the
    two takes the column and the table for injected objects and removes them with the history.
    """

    POSTGRES_DEPLOY = "postgresql"
    POSTGRES_SELECTOR = "opentelemetry.io/name=postgresql"

    # The chart caps postgres at 100Mi while shared_buffers is 128MB. Seeding the
    # history and the agent's CREATE INDEX then get it OOM-killed, and the restart
    # wipes the data. Same headroom as postgres_lock_contention_product_catalog.
    # Not restored on recovery: astronomy-shop is redeployed fresh every run.
    FAULT_MEMORY_LIMIT = "512Mi"

    # 2,700 discontinued products with 20 to 200 reviews each: 293,823 rows, a 46 MB heap. Measured at
    # steady state (local kind cluster, 512Mi): a lookup goes from ~0.05 ms to ~20 ms in Postgres and the
    # frontend median of the review endpoints from ~8 ms to ~17 ms.
    HISTORY_PRODUCTS = 2_700
    MIN_REVIEWS_PER_PRODUCT = 20
    MAX_REVIEWS_PER_PRODUCT = 200

    # Catalog product whose original reviews the frontend read check looks for.
    PROBE_PRODUCT_ID = "OLJCESPC7Z"

    # The two queries product-reviews runs
    APP_QUERIES = (
        "SELECT username, description, score FROM reviews.productreviews WHERE product_id= '{product_id}'",
        "SELECT AVG(score) FROM reviews.productreviews WHERE product_id= '{product_id}'",
    )

    USERNAME_STARTS = (
        "stargazer",
        "nightsky",
        "astro",
        "cosmic",
        "deep_sky",
        "comet",
        "nebula",
        "orbit",
        "lunar",
        "galaxy",
        "meteor",
        "zenith",
        "polaris",
        "saturn",
        "dark_sky",
        "backyard",
        "telescope",
        "celestial",
        "eclipse",
        "solar",
    )
    USERNAME_ENDS = (
        "_mike",
        "_jen",
        "_sam",
        "lover",
        "_fan",
        "_dad",
        "_mom",
        "_pro",
        "_nerd",
        "_ray",
        "_lee",
        "_kay",
        "42",
        "_99",
        "_tom",
        "_ana",
        "_watcher",
        "_chaser",
        "_guy",
        "_amy",
    )
    # (score, weight out of 100): skewed high like the shop's own reviews.
    SCORE_WEIGHTS = ((5.0, 32), (4.5, 24), (4.0, 20), (3.5, 8), (3.0, 6), (2.5, 4), (2.0, 3), (1.5, 2), (1.0, 1))
    OPENERS = {
        "good": (
            "Great value for the price.",
            "Arrived quickly and well packed.",
            "Solid build quality.",
            "Exactly as described.",
            "My kids love it.",
            "Works great for beginners.",
            "Better than I expected.",
            "Replaced an older model with this and no regrets.",
            "Bought this as a gift and they loved it.",
            "Really happy with this one.",
            "Perfect for backyard viewing.",
            "Easy to use right out of the box.",
            "Impressive for the price.",
            "Customer support was helpful.",
        ),
        "mixed": (
            "It's okay.",
            "Decent, with a few caveats.",
            "Mixed feelings about this one.",
            "Not bad for a first scope.",
            "Does what it says, mostly.",
            "Took a while to set up.",
            "Fine for casual use.",
        ),
        "bad": (
            "Not what I expected.",
            "Disappointed with this one.",
            "Had high hopes for this.",
            "Not great for the price.",
            "Wanted to like it.",
        ),
    }
    MIDDLES = {
        "good": (
            " The optics are sharp.",
            " Assembly took about an hour.",
            " It holds alignment well.",
            " Battery life is decent.",
            " Views of the moon are stunning.",
            " The case is a nice touch.",
            " It feels sturdy outdoors.",
            " Setup was painless.",
            " Saturn's rings were clearly visible.",
            " Tracking is smooth once it's aligned.",
            " Light enough to take camping.",
            " The finder scope lines up nicely.",
            " Jupiter's moons showed up on the first night.",
            " The eyepieces are better than I expected.",
        ),
        "mixed": (
            " The manual could be clearer.",
            " Focus is a little stiff.",
            " The mount wobbles in wind.",
            " Assembly took longer than expected.",
            " The tripod feels a bit flimsy.",
            " Image quality is fine but not great.",
            " The included eyepieces are basic.",
            " It needs a steady surface.",
        ),
        "bad": (
            " The finish scratches easily.",
            " It arrived with a loose part.",
            " Collimation was off out of the box.",
            " The battery drains quickly.",
            " The app kept disconnecting.",
            " The mount wobbles in wind.",
            " The focuser slips.",
        ),
    }
    CLOSERS = {
        "good": (
            " Would buy again.",
            " Recommended.",
            " Happy overall.",
            " Five stars from me.",
            " Worth it.",
            " Great first purchase.",
            " Still using it daily.",
            " Highly recommend.",
            "",
        ),
        "mixed": (" Three stars.", " Good enough for now.", " It does the job.", " Okay for the price.", ""),
        "bad": (" Returned it after a week.", " Not for serious use.", " Would not buy again.", " Look elsewhere."),
    }
    PRODUCT_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    # Where the retired products are listed, so their reviews don't look orphaned.
    DISCONTINUED_TABLE = "catalog.discontinued_products"

    # A review's date, for the checksums over rows aliased r. Read through to_jsonb, a dropped column
    # counts as changed dates rather than a query error, and the text is ISO 8601 whatever the DateStyle.
    _CREATED_AT_SQL = "COALESCE(to_jsonb(r) ->> 'created_at', '')"

    INIT_CONFIGMAP = "postgresql-init"
    INIT_KEY = "init.sql"
    # What init.sql gains, in the file's own style. _prepare_history_schema creates the same objects.
    INIT_CREATED_AT = ",\n    created_at TIMESTAMP NOT NULL DEFAULT now()"
    INIT_DISCONTINUED = (
        "\n\n-- Product Catalog Service: products no longer for sale. Their reviews stay in\n"
        "-- reviews.productreviews and are restored from the archive, not seeded here\n"
        "CREATE TABLE catalog.discontinued_products (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    name TEXT NOT NULL,\n"
        "    discontinued_on DATE NOT NULL\n"
        ");"
    )
    # Products left the catalog between 2020 and 2024; their reviews were written in the three years before.
    FIRST_DISCONTINUED = date(2020, 1, 1)
    DISCONTINUED_SPAN_DAYS = 5 * 365
    REVIEW_SPAN_DAYS = 3 * 365
    PRODUCT_BRANDS = (
        "Solaris",
        "Starfield",
        "Nightwatch",
        "Polaris",
        "Meridian",
        "Lumina",
        "Zenith",
        "Aurora",
        "Perseid",
        "Halley",
        "Kepler",
        "Cassini",
    )
    PRODUCT_SERIES = ("Explorer", "Voyager", "Pathfinder", "Observer", "Navigator", "Scout", "Pro", "Classic")
    PRODUCT_KINDS = (
        "Refractor Telescope",
        "Reflector Telescope",
        "Dobsonian Telescope",
        "Astro Binoculars",
        "Equatorial Mount",
        "Eyepiece Kit",
        "Solar Filter",
        "Star Tracker",
        "Red Flashlight",
        "Finder Scope",
        "Planetary Camera",
        "Travel Tripod",
    )

    _READY_TIMEOUT_SECONDS = 300
    _READY_POLL_SECONDS = 5
    _READY_CONSECUTIVE_OK = 3

    _PSQL_TIMEOUT_SECONDS = 180
    _PROBE_TIMEOUT_SECONDS = 30
    _SEED_TIMEOUT_SECONDS = 600

    _DDL_LOCK_TIMEOUT = "5s"
    _DDL_ATTEMPTS = 6

    def __init__(self):
        super().__init__(app=AstronomyShop())

        self.kubectl = KubeCtl()
        self.problem_id = "product_reviews_missing_index_astronomy_shop"
        self.faulty_service = ["product-reviews"]

        self.schema = "reviews"
        self.table = "productreviews"
        self.index_column = "product_id"
        self.index_name = "product_id_index"
        self._qualified = f"{self.schema}.{self.table}"

        # Captured at fault-injection time for the mitigation oracle.
        self.catalog_ids: list[str] = []
        self.baseline_review_ids: list[int] = []
        self.baseline_review_sig: str = ""
        self.table_rules_at_inject: dict[str, str] = {}
        self.probe_reviews: list[tuple[str, str]] = []
        self.probe_product_name: str = ""
        self.history_range: tuple[int, int] | None = None
        self.history_rows: int = 0
        self.history_checksum: int = 0
        self.discontinued_rows: int = 0
        self.discontinued_checksum: int = 0
        self.postgres_identity: dict | None = None
        self.heap_pages_at_inject: int = 0

        self.root_cause = self.build_structured_root_cause(
            component="table.reviews.productreviews@postgresql",
            namespace=self.namespace,
            description=(
                "The secondary index on reviews.productreviews(product_id) (product_id_index) is missing. "
                "The table holds about 294k reviews, most of them history for products no longer in the "
                "catalog, and product-reviews looks reviews up by product_id on every request "
                "(GetProductReviews: SELECT username, description, score ... WHERE product_id = ...; "
                "GetAverageProductReviewScore: SELECT AVG(score) ... WHERE product_id = ...). Without the "
                "index each lookup is a (parallel) sequential scan over the whole table, so review and "
                "product-page requests get slower and Postgres burns CPU, while every pod stays Running and "
                "the responses stay correct. The cause is only visible inside the database (EXPLAIN shows a "
                "Seq Scan, pg_stat_user_tables shows seq_scan and seq_tup_read climbing). The fix is to "
                "recreate an index on reviews.productreviews(product_id), e.g. CREATE INDEX ON "
                "reviews.productreviews (product_id), without deleting the review history."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ProductReviewsMissingIndexMitigationOracle(problem=self)
        self.app.create_workload()

    def _psql(self, sql: str, tuples_only: bool = False, timeout: float | None = None) -> str:
        flags = "-tAf -" if tuples_only else "-f -"
        script = (
            f'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
            f"-v ON_ERROR_STOP=1 {flags}"
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

    def _wait_for_reviews_ready(self, min_rows: int = 1) -> None:
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
            consecutive = consecutive + 1 if rows is not None and rows >= min_rows else 0
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

    def _catalog_ids(self) -> list[str]:
        out = self._psql("SELECT id FROM catalog.products ORDER BY id;", tuples_only=True)
        if _sql_failed(out):
            raise RuntimeError(f"Could not read catalog.products: {out.strip()}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def missing_catalog_products(self) -> list[str]:
        """Products in the catalog at injection that are gone now"""
        return sorted(set(self.catalog_ids) - set(self._catalog_ids()))

    def _max_review_id(self) -> int:
        value = _first_int(self._psql(f"SELECT COALESCE(max(id), 0) FROM {self._qualified};", tuples_only=True))
        if value is None:
            raise RuntimeError(f"Could not read max(id) from {self._qualified}")
        return value

    def _delete_history(self) -> None:
        # A re-inject without a redeploy must not stack a second history on the first,
        # or leave the old one behind as dead rows that bloat the table.
        out = self._psql(f"DELETE FROM {self._qualified} WHERE product_id NOT IN (SELECT id FROM catalog.products);")
        if "DELETE 0" not in out:
            self._psql(f"VACUUM FULL {self._qualified};", timeout=self._SEED_TIMEOUT_SECONDS)

    @classmethod
    def _hash(cls, text: str) -> int:
        return int.from_bytes(hashlib.md5(text.encode()).digest()[:4], "big")

    @classmethod
    def history_product_id(cls, n: int) -> str:
        digest = hashlib.md5(f"old-product-{n}".encode()).digest()
        return "".join(cls.PRODUCT_ID_ALPHABET[digest[k] % len(cls.PRODUCT_ID_ALPHABET)] for k in range(10))

    @classmethod
    def discontinued_products(cls, catalog_ids: list[str]) -> list[tuple[int, str, str, date]]:
        """(n, id, name, day it left the catalog) of every discontinued product"""
        catalog = set(catalog_ids)
        products = []
        for n in range(1, cls.HISTORY_PRODUCTS + 1):
            product_id = cls.history_product_id(n)
            if product_id in catalog:
                continue
            name = (
                f"{cls.PRODUCT_BRANDS[cls._hash(f'{product_id}brand') % len(cls.PRODUCT_BRANDS)]} "
                f"{cls.PRODUCT_SERIES[cls._hash(f'{product_id}series') % len(cls.PRODUCT_SERIES)]} "
                f"{60 + 10 * (cls._hash(f'{product_id}size') % 15)} "
                f"{cls.PRODUCT_KINDS[cls._hash(f'{product_id}kind') % len(cls.PRODUCT_KINDS)]}"
            )
            left = cls.FIRST_DISCONTINUED + timedelta(days=cls._hash(f"{product_id}left") % cls.DISCONTINUED_SPAN_DAYS)
            products.append((n, product_id, name, left))
        return products

    @classmethod
    def discontinued_csv(cls, catalog_ids: list[str]) -> tuple[str, int]:
        """The discontinued products (id, name, discontinued_on) as CSV, and their count"""
        buf = io.StringIO()
        products = cls.discontinued_products(catalog_ids)
        csv.writer(buf, lineterminator="\n").writerows(
            [product_id, name, left.isoformat()] for _, product_id, name, left in products
        )
        return buf.getvalue(), len(products)

    @classmethod
    def history_csv(cls, catalog_ids: list[str]) -> tuple[str, int]:
        """
        Review history for discontinued products as CSV, and its row count.

        Each review is dated within the three years before its product left the catalog.
        """
        span = cls.MAX_REVIEWS_PER_PRODUCT - cls.MIN_REVIEWS_PER_PRODUCT + 1
        scores = [score for score, weight in cls.SCORE_WEIGHTS for _ in range(weight)]
        buf, rows = io.StringIO(), 0
        writer = csv.writer(buf, lineterminator="\n")
        for n, product_id, _, left in cls.discontinued_products(catalog_ids):
            last_day = datetime(left.year, left.month, left.day)
            for r in range(1, cls.MIN_REVIEWS_PER_PRODUCT + 1 + cls._hash(f"n{n}") % span):
                key = f"{product_id}{r}"
                score = scores[cls._hash(key + "s") % len(scores)]
                mood = "good" if score >= 4.0 else "mixed" if score >= 2.5 else "bad"
                middles = cls.MIDDLES[mood]
                text = cls.OPENERS[mood][cls._hash(key + "a") % len(cls.OPENERS[mood])]
                text += middles[cls._hash(key + "b") % len(middles)]
                if cls._hash(key + "d") % 2:  # half the reviews say a bit more
                    second = middles[cls._hash(key + "e") % len(middles)]
                    text += second if second not in text else ""
                text += cls.CLOSERS[mood][cls._hash(key + "c") % len(cls.CLOSERS[mood])]
                username = cls.USERNAME_STARTS[cls._hash(key + "u") % len(cls.USERNAME_STARTS)]
                username += cls.USERNAME_ENDS[cls._hash(key + "v") % len(cls.USERNAME_ENDS)]
                if cls._hash(key + "w") % 3 == 0:
                    username += str(10 + cls._hash(key + "x") % 90)
                written = last_day - timedelta(
                    days=1 + cls._hash(key + "t") % cls.REVIEW_SPAN_DAYS, seconds=cls._hash(key + "h") % 86_400
                )
                writer.writerow([product_id, username, text, score, written.isoformat(sep=" ")])
                rows += 1
        return buf.getvalue(), rows

    @classmethod
    def init_sql_with_history(cls, sql: str) -> str:
        """init.sql with reviews.productreviews.created_at and catalog.discontinued_products added"""

        def end_of(table: str) -> int:
            start = sql.find(f"CREATE TABLE {table} (")
            end = sql.find("\n);", start) if start >= 0 else -1
            if end < 0:
                raise RuntimeError(f"init.sql has no CREATE TABLE {table} statement to extend")
            return end

        # The catalog comes after the reviews in init.sql, so edit it first to keep the offsets valid.
        if "discontinued_products" not in sql:
            at = end_of("catalog.products") + len("\n);")
            sql = sql[:at] + cls.INIT_DISCONTINUED + sql[at:]
        at = end_of("reviews.productreviews")
        if "created_at" not in sql[sql.find("CREATE TABLE reviews.productreviews (") : at]:
            sql = sql[:at] + cls.INIT_CREATED_AT + sql[at:]
        return sql

    def _patch_init_sql(self) -> None:
        """
        Put the history schema into the postgresql-init ConfigMap.

        Runs before the memory patch restarts Postgres, so the new pod builds its database from it.
        """
        current = self.kubectl.exec_command_checked(
            f"kubectl get configmap {self.INIT_CONFIGMAP} -n {self.namespace} -o json", timeout=30
        )
        try:
            sql = json.loads(current)["data"][self.INIT_KEY]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"ConfigMap {self.INIT_CONFIGMAP} has no {self.INIT_KEY}") from exc
        updated = self.init_sql_with_history(sql)
        if updated == sql:
            return
        # Written as Helm, which owns the ConfigMap, so the edit doesn't stand out in managedFields.
        self.kubectl.exec_command_checked(
            f"kubectl patch configmap {self.INIT_CONFIGMAP} -n {self.namespace} --type=merge "
            "--field-manager=helm --patch-file=/dev/stdin",
            input_data=json.dumps({"data": {self.INIT_KEY: updated}}),
            timeout=30,
        )

    def _prepare_history_schema(self) -> None:
        """
        Make the history read as history: date every review and list the discontinued products.

        After a restart init.sql has already created the column and the table (see _patch_init_sql),
        and this only empties the table. On a re-inject without a restart it creates them.

        The original reviews get dates over the last year. Left at the column default they
        would all carry the time Postgres started.
        """
        self._run_ddl(
            f"ALTER TABLE {self._qualified} ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT now(); "
            f"UPDATE {self._qualified} SET created_at = date_trunc('second', now() - id * interval '7 days 3 hours'); "
            f"CREATE TABLE IF NOT EXISTS {self.DISCONTINUED_TABLE} (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "discontinued_on DATE NOT NULL); "
            f"TRUNCATE {self.DISCONTINUED_TABLE}; "
            f"GRANT SELECT ON {self.DISCONTINUED_TABLE} TO otelu;",
            "preparing the review history tables",
        )

    def _seed_discontinued_products(self) -> None:
        data, rows = self.discontinued_csv(self.catalog_ids)
        out = self._psql(
            f"COPY {self.DISCONTINUED_TABLE} (id, name, discontinued_on) FROM STDIN WITH (FORMAT csv);\n{data}\\.\n",
            timeout=self._SEED_TIMEOUT_SECONDS,
        )
        if _sql_failed(out) or f"COPY {rows}" not in out:
            raise RuntimeError(f"Listing {rows} discontinued products failed: {out.strip()[:500]}")
        self.discontinued_rows, self.discontinued_checksum = self.discontinued_fingerprint()
        if self.discontinued_rows != rows:
            raise RuntimeError(f"Listed {rows} discontinued products but found {self.discontinued_rows}")

    def discontinued_fingerprint(self) -> tuple[int, int]:
        """Row count and content checksum of the discontinued products, (0, 0) once the table is gone"""
        present = self._psql(f"SELECT to_regclass('{self.DISCONTINUED_TABLE}') IS NOT NULL;", tuples_only=True).strip()
        answer = present.splitlines()[-1].strip() if present else ""
        if answer not in ("t", "f"):
            raise RuntimeError(f"Could not look up {self.DISCONTINUED_TABLE}: {present[:300]}")
        if answer == "f":
            return 0, 0
        # Read through to_jsonb, like created_at, so a dropped column counts as changed rows
        out = self._psql(
            "SELECT count(*) || '|' || COALESCE(sum(hashtext(concat_ws('|', j ->> 'id', j ->> 'name', "
            "j ->> 'discontinued_on'))::bigint), 0) "
            f"FROM (SELECT to_jsonb(d) AS j FROM {self.DISCONTINUED_TABLE} d) s;",
            tuples_only=True,
        )
        try:
            count, checksum = out.strip().splitlines()[-1].split("|")
            return int(count), int(checksum)
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"Could not fingerprint {self.DISCONTINUED_TABLE}: {out.strip()[:300]}") from exc

    def _seed_history(self) -> None:
        """
        Load the history with one COPY
        """
        data, rows = self.history_csv(self.catalog_ids)
        before = self._max_review_id()
        out = self._psql(
            f"COPY {self._qualified} (product_id, username, description, score, created_at) "
            "FROM STDIN WITH (FORMAT csv);\n"
            f"{data}\\.\n",
            timeout=self._SEED_TIMEOUT_SECONDS,
        )
        if _sql_failed(out) or f"COPY {rows}" not in out:
            raise RuntimeError(f"Seeding {rows} history reviews failed: {out.strip()[:500]}")

        self.history_range = (before + 1, self._max_review_id())
        self.history_rows, self.history_checksum = self.history_fingerprint()
        if self.history_rows != rows:
            raise RuntimeError(f"Seeded {rows} history reviews but found {self.history_rows} in their id range")
        logger.info("[missing-index] seeded %d history reviews (ids %s)", rows, self.history_range)

    def history_fingerprint(self) -> tuple[int, int]:
        """Row count and an order-independent content checksum of the seeded id range"""
        if self.history_range is None:
            return 0, 0
        low, high = self.history_range
        out = self._psql(
            "SELECT count(*) || '|' || COALESCE(sum(hashtext(product_id || '|' || username || '|' || "
            f"COALESCE(description, '') || '|' || score::text || '|' || {self._CREATED_AT_SQL})::bigint), 0) "
            f"FROM {self._qualified} r WHERE id BETWEEN {low} AND {high};",
            tuples_only=True,
        )
        try:
            count, checksum = out.strip().splitlines()[-1].split("|")
            return int(count), int(checksum)
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"Could not fingerprint the review history: {out.strip()[:300]}") from exc

    def product_id_indexes(self) -> list[str]:
        """Indexes on the table whose leading column is product_id, plus any index holding the name product_id_index."""
        out = self._psql(
            "SELECT i.indexrelid::regclass::text FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
            f"WHERE i.indrelid = '{self._qualified}'::regclass "
            f"AND (a.attname = '{self.index_column}' OR c.relname = '{self.index_name}');",
            tuples_only=True,
        )
        if _sql_failed(out):
            raise RuntimeError(f"Could not list the indexes on {self._qualified}: {out.strip()}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def _drop_index(self) -> None:
        for index in self.product_id_indexes():
            constraint = self._psql(
                f"SELECT conname FROM pg_constraint WHERE conindid = '{index}'::regclass;", tuples_only=True
            ).strip()
            if constraint and not _sql_failed(constraint):
                self._run_ddl(
                    f'ALTER TABLE {self._qualified} DROP CONSTRAINT "{constraint}";',
                    f"dropping constraint {constraint!r}",
                )
            else:
                self._run_ddl(f"DROP INDEX {index};", f"dropping index {index}")

    def _recreate_index(self) -> None:
        # An index on product_id doesn't prove the lookups work: a BRIN index still reads
        # most of the table, an invalid one is ignored, and a partial one only covers some products.
        self._drop_index()
        self._run_ddl(
            f"CREATE INDEX {self.index_name} ON {self._qualified} ({self.index_column});",
            "creating the product_id index",
        )

    def plan_product_ids(self) -> list[str]:
        """
        Every catalog product, one discontinued product, and one that has no reviews yet.

        The last two make an index limited to known products (a partial index
        WHERE product_id IN (...ids...)) fail: the next product added would scan again.
        """
        extra = next(pid for n in range(1, 100) if (pid := self.history_product_id(n)) not in self.catalog_ids)
        return [*self.catalog_ids, extra, self.history_product_id(self.HISTORY_PRODUCTS + 1)]

    def lookup_plans(self) -> dict[str, str]:
        """EXPLAIN of both product-reviews queries for every product in plan_product_ids(), keyed by query."""
        statements = [
            query.format(product_id=product_id) for product_id in self.plan_product_ids() for query in self.APP_QUERIES
        ]
        # One psql call; each plan is fenced by a marker line so they can be split apart.
        script = "".join(f"\\echo ---PLAN {i}\nEXPLAIN {sql};\n" for i, sql in enumerate(statements))
        out = self._psql(script, tuples_only=True)
        if _sql_failed(out):
            raise RuntimeError(f"EXPLAIN of the review lookups failed: {out.strip()[:500]}")
        plans: dict[str, list[str]] = {}
        current = None
        for line in out.splitlines():
            if line.startswith("---PLAN "):
                current = statements[int(line.split()[1])]
                plans[current] = []
            elif current is not None:
                plans[current].append(line)
        if len(plans) != len(statements):
            raise RuntimeError(f"Expected {len(statements)} plans, parsed {len(plans)}")
        return {sql: "\n".join(lines) for sql, lines in plans.items()}

    def heap_pages(self) -> int:
        value = _first_int(self._psql(f"SELECT pg_relation_size('{self._qualified}') / 8192;", tuples_only=True))
        if value is None:
            raise RuntimeError(f"Could not read the size of {self._qualified}")
        return value

    # Which server and database a connection is talking to
    DATABASE_TARGET_SQL = "SELECT current_database() || '|' || extract(epoch FROM pg_postmaster_start_time())"

    def database_target(self) -> str:
        """The server and database the oracle's own data checks read."""
        out = self._psql(f"{self.DATABASE_TARGET_SQL};", tuples_only=True).strip()
        if not out or _sql_failed(out):
            raise RuntimeError(f"Could not identify the postgres database: {out[:300]}")
        return out.splitlines()[-1].strip()

    def lookup_costs(self) -> tuple[str, dict[str, dict]]:
        """
        Run both product-reviews queries for every product in plan_product_ids() exactly the way the app does.

        From inside the product-reviews pod, over its own DB_CONNECTION_STRING (same user, database,
        options and role settings), under EXPLAIN (ANALYZE, BUFFERS). Returns the database the app is
        connected to (see DATABASE_TARGET_SQL) and, per query, how many shared buffers it touched and its
        plan node types. The buffer count is what tells a real index lookup (a handful of pages) from an
        index that still reads most of the table (a BRIN index, or a btree with product_id second scanned
        end to end).
        """
        statements = [
            query.format(product_id=product_id) for product_id in self.plan_product_ids() for query in self.APP_QUERIES
        ]
        script = (
            "import json, os, psycopg2\n"
            "connection = psycopg2.connect(os.environ['DB_CONNECTION_STRING'], connect_timeout=10)\n"
            "plans = {}\n"
            "try:\n"
            "    with connection.cursor() as cursor:\n"
            "        cursor.execute(\"SET statement_timeout = '60s'\")\n"
            f"        cursor.execute({self.DATABASE_TARGET_SQL!r})\n"
            "        target = cursor.fetchone()[0]\n"
            f"        for sql in {statements!r}:\n"
            "            cursor.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql)\n"
            "            plans[sql] = cursor.fetchone()[0]\n"
            "    connection.rollback()\n"
            "finally:\n"
            "    connection.close()\n"
            "print('---PLANS ' + json.dumps({'target': target, 'plans': plans}))\n"
        )
        out = self.kubectl.exec_command(
            f"kubectl exec -n {self.namespace} deploy/product-reviews -c product-reviews -- "
            f"/venv/bin/python -c {shlex.quote(script)}"
        )
        marker = next((line for line in out.splitlines() if line.startswith("---PLANS ")), None)
        if marker is None:
            raise RuntimeError(f"EXPLAIN ANALYZE of the review lookups failed: {out.strip()[-500:]}")
        try:
            found = json.loads(marker[len("---PLANS ") :])
            target, plans = found["target"], found["plans"]
            costs = {}
            for sql in statements:
                plan = plans[sql][0]["Plan"]
                nodes, stack = [], [plan]
                while stack:
                    node = stack.pop()
                    nodes.append(node.get("Node Type", ""))
                    stack.extend(node.get("Plans", []))
                costs[sql] = {
                    "buffers": plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0),
                    "nodes": nodes,
                }
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Could not parse the lookup plans: {marker[:300]}") from exc
        return target, costs

    @staticmethod
    def plan_uses_index_scan(plan: str) -> bool:
        # Index Scan, Index Only Scan and Bitmap Index Scan all count (the planner picks a
        # bitmap plan for product_id lookups), so any index type with product_id first works.
        lowered = plan.lower()
        return ("index scan" in lowered or "index only scan" in lowered) and "seq scan" not in lowered

    def _reviews_for(self, product_id: str) -> list[tuple[str, str]]:
        out = self._psql(
            f"SELECT username, description FROM {self._qualified} WHERE product_id = '{product_id}' ORDER BY id;",
            tuples_only=True,
        )
        return [tuple(line.split("|", 1)) for line in out.splitlines() if "|" in line]

    def _review_data_signature(self, ids: list[int]) -> str:
        """md5 over (id, product_id, username, description, score, created_at) of the given rows"""
        if not ids:
            return ""
        id_list = ",".join(str(i) for i in ids)
        out = self._psql(
            "SELECT md5(COALESCE(string_agg("
            "id || '|' || product_id || '|' || username || '|' || COALESCE(description, '') || '|' || score "
            f"|| '|' || {self._CREATED_AT_SQL}, ',' ORDER BY id), '')) "
            f"FROM {self._qualified} r WHERE id IN ({id_list});",
            tuples_only=True,
        ).strip()
        if _sql_failed(out):
            raise RuntimeError(f"Could not read the original reviews: {out[:300]}")
        return out.splitlines()[-1].strip() if out else ""

    def _capture_review_baseline(self) -> tuple[list[int], str]:
        """Ids and content signature of the original reviews, before the history is seeded"""
        out = self._psql(f"SELECT id FROM {self._qualified} ORDER BY id;", tuples_only=True)
        ids = [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
        return ids, self._review_data_signature(ids)

    def table_rules(self) -> dict[str, str]:
        """
        Per column: NOT NULL, a default or identity, and uniqueness. For the table:
        row-level security, logged storage, and whether rows can sit in an inheritance child.

        Uniqueness needs a live, non-partial unique index with that column as its only key (normally
        productreviews_pkey). A table rebuilt without these keeps every row, so the data checks can't see the
        loss, and they run as the superuser, which row-level security doesn't apply to. A child table sits
        outside the primary key.
        """
        out = self._psql(
            "SELECT a.attname || '|' || a.attnotnull || '|' || (a.atthasdef OR a.attidentity <> '') || '|' || EXISTS ("
            "SELECT 1 FROM pg_index i WHERE i.indrelid = a.attrelid AND i.indisunique AND i.indisvalid "
            "AND i.indisready AND i.indislive AND i.indpred IS NULL AND i.indnkeyatts = 1 "
            "AND i.indkey[0] = a.attnum) "
            f"FROM pg_attribute a WHERE a.attrelid = '{self._qualified}'::regclass "
            "AND a.attnum > 0 AND NOT a.attisdropped "
            "UNION ALL SELECT '(table)|' || c.relrowsecurity || '|' || c.relpersistence::text || '|' || "
            "(c.relkind = 'r' AND EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhparent = c.oid)) "
            f"FROM pg_class c WHERE c.oid = '{self._qualified}'::regclass;",
            tuples_only=True,
        )
        if _sql_failed(out):
            raise RuntimeError(f"Could not read the rules on {self._qualified}: {out.strip()[:300]}")
        return dict(line.strip().split("|", 1) for line in out.splitlines() if "|" in line)

    def original_reviews_intact(self) -> tuple[bool, str]:
        """True if every original review still exists unchanged"""
        if not self.baseline_review_ids:
            return True, "no baseline captured"
        expected = len(self.baseline_review_ids)
        id_list = ",".join(str(i) for i in self.baseline_review_ids)
        present = _first_int(
            self._psql(f"SELECT count(*) FROM {self._qualified} WHERE id IN ({id_list});", tuples_only=True)
        )
        if present is None:
            raise RuntimeError("Could not count the original reviews")
        if present != expected:
            return False, f"{expected - present} of {expected} original reviews are gone"
        if self._review_data_signature(self.baseline_review_ids) != self.baseline_review_sig:
            return False, "original review contents were modified"
        return True, "intact"

    def review_reads_work(self) -> bool:
        """Read the probe product and its original reviews through the frontend"""
        expected = [{"username": u, "description": d} for u, d in self.probe_reviews]
        script = (
            "import json, requests; "
            f"p=requests.get('http://frontend:8080/api/products/{self.PROBE_PRODUCT_ID}', timeout=30); "
            "p.raise_for_status(); product=p.json(); "
            f"assert product.get('id') == {self.PROBE_PRODUCT_ID!r}; "
            f"assert product.get('name') == {self.probe_product_name!r}; "
            f"r=requests.get('http://frontend:8080/api/product-reviews/{self.PROBE_PRODUCT_ID}', timeout=30); "
            "r.raise_for_status(); rows=r.json(); "
            "seen={(row.get('username'), row.get('description')) for row in rows}; "
            f"expected=json.loads({json.dumps(json.dumps(expected))}); "
            "assert all((e['username'], e['description']) in seen for e in expected); "
            "print('reviews-read-ok')"
        )
        output = self.kubectl.exec_command(
            f"kubectl exec -n {self.namespace} deploy/load-generator -c load-generator -- "
            f"python -c {shlex.quote(script)}"
        )
        return output.strip() == "reviews-read-ok"

    def _postgres_pods(self) -> list[dict] | None:
        out = self.kubectl.exec_command(f"kubectl get pods -n {self.namespace} -l {self.POSTGRES_SELECTOR} -o json")
        try:
            return [p for p in json.loads(out)["items"] if not p["metadata"].get("deletionTimestamp")]
        except (ValueError, KeyError):
            return None

    def postgres_pod_count(self) -> int:
        pods = self._postgres_pods()
        if pods is None:
            raise RuntimeError("Could not list the postgres pods")
        return len(pods)

    def current_postgres_identity(self) -> dict | None:
        """uid, restart count and last termination reason of the running postgres pod"""
        pods = self._postgres_pods()
        if pods is None or len(pods) != 1:
            return None
        status = (pods[0].get("status", {}).get("containerStatuses") or [{}])[0]
        return {
            "uid": pods[0]["metadata"]["uid"],
            "restarts": status.get("restartCount", 0),
            "last_reason": ((status.get("lastState") or {}).get("terminated") or {}).get("reason"),
        }

    def _namespace_exists(self) -> bool:
        out = self.kubectl.exec_command(f"kubectl get namespace {self.namespace} --no-headers --ignore-not-found")
        return self.namespace in out

    @mark_fault_injected
    def inject_fault(self):
        logger.info("Injecting product-reviews missing-index fault...")

        self._patch_init_sql()
        self._set_postgres_memory(self.FAULT_MEMORY_LIMIT)
        self._wait_for_reviews_ready()

        self.catalog_ids = self._catalog_ids()
        if not self.catalog_ids:
            raise RuntimeError("catalog.products is empty. It cannot pick products for the plan checks")

        self._delete_history()
        self._prepare_history_schema()
        self._recreate_index()

        self.baseline_review_ids, self.baseline_review_sig = self._capture_review_baseline()
        self.table_rules_at_inject = self.table_rules()
        self.probe_reviews = self._reviews_for(self.PROBE_PRODUCT_ID)
        self.probe_product_name = self._psql(
            f"SELECT name FROM catalog.products WHERE id = '{self.PROBE_PRODUCT_ID}';", tuples_only=True
        ).strip()
        if not self.probe_reviews or not self.probe_product_name or _sql_failed(self.probe_product_name):
            raise RuntimeError(f"product {self.PROBE_PRODUCT_ID} has no name or reviews to probe through the frontend")

        self._seed_discontinued_products()
        self._seed_history()
        self._analyze()
        # The oracle's buffer limit is a fraction of this, so bloating the table later can't raise it.
        self.heap_pages_at_inject = self.heap_pages()

        plans = self.lookup_plans()
        if not all(self.plan_uses_index_scan(plan) for plan in plans.values()):
            raise RuntimeError(f"Lookups did not use {self.index_name} before the fault; nothing to break:\n{plans}")

        self._drop_index()
        self._analyze()
        plans = self.lookup_plans()
        if not all("seq scan" in plan.lower() for plan in plans.values()):
            raise RuntimeError(f"Expected Seq Scans after dropping the index, got:\n{plans}")

        self.postgres_identity = self.current_postgres_identity()

        logger.info(
            "product-reviews missing-index fault injected (%d history rows, Seq Scan confirmed).", self.history_rows
        )
        return True

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        logger.info("Recovering from product-reviews missing-index fault...")
        if not self._namespace_exists():
            logger.info("Namespace '%s' not present; nothing to recover", self.namespace)
            return True

        # A failed recovery marks the attempt incomplete, which would hide an agent's failure. If the agent
        # dropped the table or scaled postgres to 0 there's nothing to rebuild, and the app is undeployed next.
        try:
            self._wait_for_reviews_ready(min_rows=0)
        except RuntimeError as exc:
            logger.warning("Could not reach %s to recover it: %s", self._qualified, exc)
            return False

        self._recreate_index()
        self._analyze()

        # A table of a few pages (init.sql's 50 rows after a postgres restart) is read with a Seq Scan
        # whatever its indexes, and the oracle allows that too.
        if self.heap_pages() > ProductReviewsMissingIndexMitigationOracle.MIN_LOOKUP_BUFFERS:
            plans = self.lookup_plans()
            if not all(self.plan_uses_index_scan(plan) for plan in plans.values()):
                raise RuntimeError(f"Index restored but the lookups still don't use it:\n{plans}")

        # NOTE: the memory limit is not restored. Restoring it restarts postgres,
        # which wipes the tables, and astronomy-shop is redeployed fresh every run.
        logger.info("product-reviews missing-index fault recovered.")
        return True
