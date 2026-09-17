import logging
import os

from sregym.conductor.oracles.integer_overflow_primary_key_mitigation import IntegerOverflowPrimaryKeyMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class IntegerOverflowPrimaryKeyAstronomyShop(Problem):
    """
    Fault:

    reviews.productreviews.id is an INTEGER (int4) identity column whose sequence maxes out
    at 2,147,483,647. Once the sequence reaches that limit, every new INSERT that allocates
    an id via nextval() fails with "nextval: reached maximum value of sequence",
    so product-review writes stop.

    Existing reads remain healthy, and every pod (product-reviews and postgres) stays
    Running with normal CPU and memory, so nothing crashes. The cause is only visible inside
    the database (the id column's data type and the sequence's last_value), which is why it is
    hard to diagnose from pod health.

    The durable fix is a schema migration widening the id column and its sequence
    to BIGINT (ALTER COLUMN id TYPE bigint).
    """

    POSTGRES_DEPLOY = "postgresql"

    PG_SUPERUSER = "root"
    # The application role product-reviews connects as (see postgresql/init.sql).
    PG_APP_USER = "otelu"
    PG_APP_PASSWORD = "otelp"
    PG_DB = "otel"
    SEQUENCE = "reviews.productreviews_id_seq"
    INT4_MAX = 2147483647

    # A marker review inserted at fault-injection time, disguised as a genuine
    # review (a real product_id, a plausible handle, an ordinary blurb) so an
    # agent scanning the table cannot single it out and preserve it. The oracle
    # matches it by content. A DB re-seed (postgres pod restart), TRUNCATE or DROP
    # restores the seeded rows but not this one, so its absence flags them.
    SENTINEL_PRODUCT_ID = "OLJCESPC7Z"
    SENTINEL_USERNAME = "orion_hunter"
    SENTINEL_DESCRIPTION = (
        "Crisp optics and a rock-steady mount. Tracked Saturn for an hour and the rings were razor sharp."
    )
    SENTINEL_SCORE = "4.5"

    def __init__(self):
        super().__init__(app=AstronomyShop())

        self.kubectl = KubeCtl()
        self.problem_id = "integer_overflow_primary_key_astronomy_shop"
        self.faulty_service = ["product-reviews"]

        # Captured at fault-injection time so the mitigation oracle can verify the
        # original seeded reviews survived intact.
        self._baseline_review_ids: list[int] | None = None
        self._baseline_review_sig: str | None = None

        # Starts a review-submission workload during fault injection so write failures
        # come from the application user, not just the oracle's checks.
        self.writer_manifest = os.path.join(os.path.dirname(__file__), "manifests", "review_writer.yaml")

        self.root_cause = self.build_structured_root_cause(
            component="table.reviews.productreviews@postgresql",
            namespace=self.namespace,
            description=(
                "The identity sequence backing reviews.productreviews.id (an INTEGER/int4 "
                "column) has reached its maximum value of 2147483647. Every new product-review "
                "INSERT fails when nextval() overflows with 'reached maximum value of sequence', "
                "while the reads of existing reviews stay healthy and all pods remain Running. "
                "The durable fix is a schema migration widening the id column and its sequence "
                "to BIGINT (ALTER TABLE reviews.productreviews ALTER COLUMN id TYPE bigint)."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = IntegerOverflowPrimaryKeyMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self) -> bool:
        logger.info("Injecting integer-overflow (sequence exhaustion) fault...")

        # Snapshot the original seeded reviews (their ids + a content signature)
        # while the table is pristine, so the oracle can later confirm a "fix"
        # neither deleted nor rewrote them.
        self._baseline_review_ids, self._baseline_review_sig = self._capture_review_baseline()

        # Insert a marker review while the sequence is still healthy. It looks
        # like an ordinary review so an agent cannot spot and preserve it
        # specifically; the oracle matches it by content. Re-seeding the DB
        # (deleting the postgres pod), TRUNCATE or DROP restores the seeded rows
        # but not this one, so the oracle can reject those actions.
        self._insert_sentinel_row()

        # Pin the identity sequence at INT4 max (is_called=true) so the very next
        # nextval() overflows and product-review writes start failing
        self._run_sql(f"SELECT setval('{self.SEQUENCE}', {self.INT4_MAX}, true);")

        # Fail if the fault did not actually take
        # A 'write' must now fail
        status = self._review_write_status()
        if status != "exhausted":
            raise RuntimeError(f"sequence exhaustion not confirmed (write status={status})")

        # Start the review-submission workload
        self.kubectl.apply_configs(self.namespace, self.writer_manifest)

        logger.info("Integer-overflow fault Injected.")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        logger.info("Recovering from integer-overflow fault...")

        # Stop the review-submission workload first
        self.kubectl.delete_configs(self.namespace, self.writer_manifest)
        self.kubectl.exec_command(
            f"kubectl delete pod -n {self.namespace} -l app=review-writer --force --grace-period=0 --ignore-not-found"
        )

        # If the app namespace is gone already, there is nothing to recover
        if not self._namespace_exists():
            logger.info("Namespace '%s' not present; nothing to recover", self.namespace)
            return True

        # The real SRE mitigation: widen the id column and its sequence to BIGINT,
        # so that nextval() has headroom again.
        self._run_sql(
            "ALTER TABLE reviews.productreviews ALTER COLUMN id TYPE bigint; "
            "ALTER SEQUENCE reviews.productreviews_id_seq AS bigint MAXVALUE 9223372036854775807;"
        )

        # Confirm writes are restored, fail if the mitigation didn't help
        status = self._review_write_status()
        if status != "ok":
            raise RuntimeError(f"writes not restored after migration (status={status})")

        # NOTE: we deliberately do not reset the sequence value. astronomy-shop is undeployed
        # and redeployed fresh every run, so the exhausted sequence never leaks across runs.
        logger.info("Integer-overflow fault recovered")
        return True

    def _run_sql(self, query: str) -> None:
        """Run a SQL command as the postgres superuser"""

        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f'env PGPASSWORD=otel psql -U {self.PG_SUPERUSER} -d {self.PG_DB} -c "{query}"'
        )

        self.kubectl.exec_command_checked(cmd)

    def _review_write_status(self) -> str:
        """
        Probe whether reviews.productreviews accepts a write FROM THE APPLICATION
        USER (otelu), without persisting a row.

        The probe connects as otelu -- the same role product-reviews uses -- rather
        than the superuser, so a "fix" that repairs the sequence as root while the
        app user's write path stays broken (for example its INSERT privilege was
        revoked) is still caught. The INSERT runs inside a rolled-back transaction,
        so a successful probe leaves no data. Returns one of:
            "ok"        - the insert succeeded
            "exhausted" - the id sequence overflowed / has no room left
            "denied"    - otelu lacks privilege to insert (write path still broken)
            "collision" - the insert hit a duplicate id (sequence over occupied ids)
            "other"     - some other failure (postgres restarting, table missing, ...)
        """

        sql = (
            "BEGIN; "
            "INSERT INTO reviews.productreviews (product_id, username, description, score) "
            "VALUES ('OLJCESPC7Z', 'app_write_probe', 'application write probe', 5.0); "
            "ROLLBACK;"
        )

        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f"env PGPASSWORD={self.PG_APP_PASSWORD} psql -h 127.0.0.1 -U {self.PG_APP_USER} -d {self.PG_DB} "
            f'-v ON_ERROR_STOP=1 -c "{sql}"'
        )

        out = self.kubectl.exec_command(cmd).lower()
        if "insert 0 1" in out:
            return "ok"
        if "reached maximum value of sequence" in out or "integer out of range" in out:
            return "exhausted"
        if "permission denied" in out:
            return "denied"
        if "duplicate key" in out or "unique constraint" in out:
            return "collision"
        return "other"

    def _insert_sentinel_row(self) -> None:
        """Insert the disguised marker review (the oracle matches it by content)."""
        self._run_sql(
            "INSERT INTO reviews.productreviews (product_id, username, description, score) "
            f"VALUES ('{self.SENTINEL_PRODUCT_ID}', '{self.SENTINEL_USERNAME}', "
            f"'{self.SENTINEL_DESCRIPTION}', {self.SENTINEL_SCORE});"
        )

    def _review_sentinel_present(self) -> bool:
        """
        True if the marker review injected with the fault is still present.

        Matched by its (disguised, normal-looking) content rather than any
        tell-tale text or its id. Content matching is what makes this re-seed
        proof: a re-seed restores the original rows and its fresh sequence can
        re-issue the marker's old id to a brand-new writer row, so an id-only
        check would be fooled -- but no re-seeded or app-written row carries this
        exact username+description. A real fix leaves the row untouched.
        """
        out = self._psql_super(
            "SELECT count(*) FROM reviews.productreviews "
            f"WHERE username = '{self.SENTINEL_USERNAME}' AND description = '{self.SENTINEL_DESCRIPTION}';",
            tuples_only=True,
        ).strip()
        try:
            return int(out.splitlines()[-1].strip()) >= 1
        except (ValueError, IndexError):
            return False

    def _review_row_count(self) -> int:
        """Number of rows currently in reviews.productreviews (-1 if unreadable)."""
        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f"env PGPASSWORD=otel psql -U {self.PG_SUPERUSER} -d {self.PG_DB} "
            f'-tA -c "SELECT count(*) FROM reviews.productreviews;"'
        )
        out = self.kubectl.exec_command(cmd).strip()
        try:
            return int(out.splitlines()[-1].strip())
        except (ValueError, IndexError):
            return -1

    def _review_id_headroom(self) -> int:
        """
        Number of IDs remaining before the identity sequence hits its limit (seqmax - last_value).

        This should be a very large number after a BIGINT migration. If it's still tiny,
        the sequence was probably just reset instead of being fixed correctly.

        This helps the check make sure that the sequence was fixed properly, instead of
        only verifying that one insert happened to work.

        Returns -1 if the value can't be read.
        """
        sql = (
            f"SELECT (SELECT seqmax FROM pg_sequence WHERE seqrelid = '{self.SEQUENCE}'::regclass) "
            f"- (SELECT last_value FROM {self.SEQUENCE});"
        )
        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f"env PGPASSWORD=otel psql -U {self.PG_SUPERUSER} -d {self.PG_DB} "
            f'-tA -c "{sql}"'
        )
        out = self.kubectl.exec_command(cmd).strip()
        try:
            return int(out.splitlines()[-1].strip())
        except (ValueError, IndexError):
            return -1

    def _psql_super(self, query: str, tuples_only: bool = False) -> str:
        """Run a read/DDL query as the postgres superuser and return its output.

        Unlike _run_sql this does not raise on a non-zero exit, so callers can
        inspect error text (permission denied, duplicate key, ...). Pass
        tuples_only=True (-tA) for bare scalar/column output.
        """
        flags = "-tA " if tuples_only else ""
        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f'env PGPASSWORD=otel psql -U {self.PG_SUPERUSER} -d {self.PG_DB} {flags}-c "{query}"'
        )
        return self.kubectl.exec_command(cmd)

    def _review_data_signature(self, ids: list[int]) -> str:
        """md5 over (id, product_id, username, description, score) of the given rows.

        A stable fingerprint of specific review rows: deleting or editing any of
        them changes it. Empty string if ``ids`` is empty.
        """
        if not ids:
            return ""
        id_list = ",".join(str(i) for i in ids)
        sql = (
            "SELECT md5(COALESCE(string_agg("
            "id || '|' || product_id || '|' || username || '|' || COALESCE(description, '') || '|' || score, "
            "',' ORDER BY id), '')) "
            f"FROM reviews.productreviews WHERE id IN ({id_list});"
        )
        out = self._psql_super(sql, tuples_only=True).strip()
        return out.splitlines()[-1].strip() if out else ""

    def _capture_review_baseline(self) -> tuple[list[int], str]:
        """Snapshot the ids and content signature of the reviews present now.

        Called at inject time while the table still holds only the original seed,
        so the oracle can later confirm those rows survived a candidate fix.
        """
        out = self._psql_super("SELECT id FROM reviews.productreviews ORDER BY id;", tuples_only=True)
        ids = [int(line.strip()) for line in out.splitlines() if line.strip().lstrip("-").isdigit()]
        return ids, self._review_data_signature(ids)

    def _original_reviews_intact(self) -> tuple[bool, str]:
        """True if every originally seeded review still exists unchanged.

        Guards against a "fix" that deletes the seeded reviews and pads the table
        back up with fresh rows: the baseline ids would be missing or their
        content signature would differ. Returns (ok, human-readable detail).
        """
        if not self._baseline_review_ids:
            return True, "no baseline captured"
        expected = len(self._baseline_review_ids)
        id_list = ",".join(str(i) for i in self._baseline_review_ids)
        out = self._psql_super(
            f"SELECT count(*) FROM reviews.productreviews WHERE id IN ({id_list});", tuples_only=True
        ).strip()
        try:
            present = int(out.splitlines()[-1].strip())
        except (ValueError, IndexError):
            return False, "could not read the reviews table"
        if present != expected:
            return False, f"{expected - present} of {expected} original reviews are gone"
        if self._review_data_signature(self._baseline_review_ids) != self._baseline_review_sig:
            return False, "original review contents were modified"
        return True, "intact"

    def _namespace_exists(self) -> bool:
        """True if the problem's app namespace currently exists"""

        out = self.kubectl.exec_command(f"kubectl get namespace {self.namespace} --no-headers --ignore-not-found")
        return self.namespace in out
