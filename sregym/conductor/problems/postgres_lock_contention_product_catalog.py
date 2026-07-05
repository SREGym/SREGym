import logging
import os
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.postgres_lock_mitigation import PostgresLockMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class PostgresLockContentionProductCatalog(Problem):
    """
    Fault:

    If one database session holds an ACCESS EXCLUSIVE lock on product-catalog's catalog.products
    table and doesn't release it, every read of that table queues behind the lock and never completes.

    product-catalog and the four services that call it (frontend, checkout, recommendation, product-reviews) hangs.
    The product-catalog pod is Running, and Postgres itself is up with normal CPU and memory, but nothing crashes.

    The cause is only visible inside the database (pg_locks / pg_stat_activity),
    which is why it isn’t easy to diagnose the issue.

    The fix is to end the blocking session (pg_terminate_backend on its pid) or remove the process
    holding the lock.
    """

    POSTGRES_DEPLOY = "postgresql"
    # postgres ships with a 100Mi limit and gets OOMKilled under the connection pile-up within minutes,
    # which would restart it and release the lock (the fault would self-destruct).

    # Raising the limit so the fault is pure lock contention. (We do NOT restore it on recovery: see recover_fault.)
    FAULT_MEMORY_LIMIT = "512Mi"

    def __init__(self):
        super().__init__(app=AstronomyShop())

        self.kubectl = KubeCtl()
        self.problem_id = "postgres_lock_contention_product_catalog"
        self.faulty_service = ["product-catalog"]
        self.manifest_path = os.path.join(os.path.dirname(__file__), "manifests", "pg_lock_holder.yaml")

        self.root_cause = self.build_structured_root_cause(
            component="postgresql (table catalog.products)",
            namespace=self.namespace,
            description=(
                "If one database session holds an ACCESS EXCLUSIVE lock on product-catalog's catalog.products table and doesn't release it,"
                "every read of that table queues behind the lock and never completes. "
                "product-catalog and the four services that call it "
                "(frontend, checkout, recommendation, product-reviews) hangs."
                "The product-catalog pod is Running, and Postgres itself is up with normal CPU and memory, but nothing crashes."
                "The cause is only visible inside the database (pg_locks / pg_stat_activity), "
                "which is why it isn’t easy to diagnose the issue."
                "The fix is to end the blocking session (pg_terminate_backend on its pid) or "
                "remove the process holding the lock."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = PostgresLockMitigationOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self) -> bool:
        logger.info("Injecting Postgres lock-contention fault...")

        # Give postgres headroom so the connection pile-up behind the lock
        # does not OOM-kill it.
        self._set_postgres_memory(self.FAULT_MEMORY_LIMIT)
        self.kubectl.exec_command(
            f"kubectl rollout status deploy/{self.POSTGRES_DEPLOY} -n {self.namespace} --timeout=180s"
        )

        # Deploying the lock-holder (takes ACCESS EXCLUSIVE on catalog.products).
        self.kubectl.apply_configs(self.namespace, self.manifest_path)

        if not self._wait_until(lambda: self._catalog_read_status() == "locked", timeout=180):
            logger.warning("Lock not confirmed held within timeout; fault may not be fully established.")

        logger.info("Postgres lock-contention fault injected.")
        return True

    @mark_fault_injected
    def recover_fault(self) -> bool:
        logger.info("Recovering from Postgres lock-contention fault...")

        # If the app namespace is already gone (e.g. an upstream deploy failure
        # tore it down), there is nothing to recover. So we return early instead of
        # flooding errors against a missing namespace.
        if not self._namespace_exists():
            logger.info("Namespace '%s' not present; nothing to recover.", self.namespace)
            return True

        # Stopping the lock-holder from reconnecting: delete the Job and force-delete
        # its pod (otherwise its shell loop simply re-acquires the lock).
        self.kubectl.delete_configs(self.namespace, self.manifest_path)
        self.kubectl.exec_command(
            f"kubectl delete pod -n {self.namespace} "
            "-l sregym-fault=postgres-lock-contention --force --grace-period=0 --ignore-not-found"
        )

        # Deleting the pod is NOT enough: the Postgres backend holding the lock is
        # parked in pg_sleep() and never notices the client is gone.

        # Actively terminate that backend: the same action an SRE/agent
        # takes (pg_terminate_backend), retrying until reads succeed again.
        def released() -> bool:
            self._terminate_lock_holder_backend()
            return self._catalog_read_status() == "ok"

        self._wait_until(released, timeout=120)

        logger.info("Postgres lock-contention fault recovered.")
        return True

    """
    Terminate any backend holding an ACCESS EXCLUSIVE lock on
    catalog.products, which releases the lock immediately.
    """

    def _terminate_lock_holder_backend(self) -> None:
        sql = (
            "SELECT pg_terminate_backend(l.pid) "
            "FROM pg_locks l "
            "JOIN pg_class c ON l.relation = c.oid "
            "JOIN pg_namespace n ON c.relnamespace = n.oid "
            "WHERE n.nspname = 'catalog' AND c.relname = 'products' "
            "AND l.mode = 'AccessExclusiveLock' AND l.pid <> pg_backend_pid();"
        )
        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            f'env PGPASSWORD=otel psql -U root -d otel -c "{sql}"'
        )
        self.kubectl.exec_command(cmd)

    """
    Patch the postgres container's memory limit (index-based JSON patch)
    """

    def _set_postgres_memory(self, memory: str) -> None:
        patch = (
            f"kubectl -n {self.namespace} patch deploy {self.POSTGRES_DEPLOY} --type=json "
            f'-p=\'[{{"op":"replace",'
            f'"path":"/spec/template/spec/containers/0/resources/limits/memory",'
            f'"value":"{memory}"}}]\''
        )
        self.kubectl.exec_command(patch)

    """ True if the problem's app namespace currently exists. """

    def _namespace_exists(self) -> bool:
        out = self.kubectl.exec_command(f"kubectl get namespace {self.namespace} --no-headers --ignore-not-found")
        return self.namespace in out

    """
    Probe whether catalog.products is readable:

    Runs a read with a short lock_timeout and echoes a READ_OK sentinel only
    when psql exits 0, so a failing query (lock timeout, missing table,
    connection error) can never be mistaken for success by matching text.

    Returns one of:
        "ok"     - the table was read (no conflicting lock held)
        "locked" - the read was blocked by a lock (lock_timeout fired)
        "other"  - some other failure (postgres restarting, table missing, ...)
    """

    def _catalog_read_status(self) -> str:
        cmd = (
            f"kubectl exec -n {self.namespace} deploy/{self.POSTGRES_DEPLOY} -- "
            'sh -c "env PGPASSWORD=otel psql -U root -d otel -v ON_ERROR_STOP=1 -t -A '
            "-c 'SET lock_timeout = 3000;' "
            "-c 'SELECT 1 FROM catalog.products LIMIT 1;' && echo READ_OK\""
        )
        out = self.kubectl.exec_command(cmd)
        if "READ_OK" in out:
            return "ok"
        if "lock timeout" in out.lower() or "canceling statement due to lock" in out.lower():
            return "locked"
        return "other"

    """ Poll `predicate` until it returns True or `timeout` seconds elapse. """

    @staticmethod
    def _wait_until(predicate, timeout: int = 120, interval: int = 3) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False
