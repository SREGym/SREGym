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
    PG_DB = "otel"
    SEQUENCE = "reviews.productreviews_id_seq"
    INT4_MAX = 2147483647

    # Marker row inserted when the fault is triggered. A proper fix should leave it
    # untouched. If the database is re-seeded (for example, by deleting the postgres
    # pod) or the table is TRUNCATED, this row disappears allowing the oracle to
    # reject those as invalid mitigations.
    SENTINEL_MARKER = "seq_overflow_sentinel"

    def __init__(self):
        super().__init__(app=AstronomyShop())

        self.kubectl = KubeCtl()
        self.problem_id = "integer_overflow_primary_key_astronomy_shop"
        self.faulty_service = ["product-reviews"]

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

        # Insert a sentinel row while the sequence is still healthy.
        # re-seeding the DB (deleting the postgres pod) or TRUNCATE wipes it,
        # so the oracle can reject those actions.
        self._run_sql(
            "INSERT INTO reviews.productreviews (product_id, username, description, score) "
            f"VALUES ('SENTINEL', '{self.SENTINEL_MARKER}', 'fault marker row; do not delete', 5.0);"
        )

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
        Probe whether reviews.productreviews accepts a write, without persisting a row.

        Runs INSERT inside a transaction that is rolled back, so a successful probe leaves no
        data. Returns one of:
            "ok"        - the insert succeeded (sequence has headroom)
            "exhausted" - the insert failed because the id sequence overflowed
            "other"     - some other failure (postgres restarting, table missing, ...)
        """

        sql = (
            "BEGIN; "
            "INSERT INTO reviews.productreviews (product_id, username, description, score) "
            "VALUES ('PROBE', 'health_probe', 'oracle smoke', 5.0); "
            "ROLLBACK;"
        )

        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f"env PGPASSWORD=otel psql -U {self.PG_SUPERUSER} -d {self.PG_DB} "
            f'-v ON_ERROR_STOP=1 -c "{sql}"'
        )

        out = self.kubectl.exec_command(cmd).lower()
        if "insert 0 1" in out:
            return "ok"
        if "reached maximum value of sequence" in out or "integer out of range" in out:
            return "exhausted"

        return "other"

    def _review_sentinel_present(self) -> bool:
        """
        True if the fault sentinel row is still present.

        Injected while the sequence was healthy. A real fix should preserve it.
        If this row disappears, the DB was likely reset or wiped, so the 'fix'
        is rejected because it destroyed the existing data.
        """
        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f"env PGPASSWORD=otel psql -U {self.PG_SUPERUSER} -d {self.PG_DB} "
            f"-tA -c \"SELECT count(*) FROM reviews.productreviews WHERE username = '{self.SENTINEL_MARKER}';\""
        )
        out = self.kubectl.exec_command(cmd).strip()
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

    def _namespace_exists(self) -> bool:
        """True if the problem's app namespace currently exists"""

        out = self.kubectl.exec_command(f"kubectl get namespace {self.namespace} --no-headers --ignore-not-found")
        return self.namespace in out
