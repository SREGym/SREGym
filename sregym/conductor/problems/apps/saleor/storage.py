"""Saleor: storage."""

from __future__ import annotations

import json
import time

from sregym.conductor.oracles.apps.saleor.state import SaleorFaultStateOracle
from sregym.conductor.problems.apps.saleor.helpers import (
    API,
    APP,
    HIDDEN_NAMESPACE,
    POSTGRES,
    WORKER,
    ensure_namespace,
    require_checkout,
    state_file,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port
from sregym.utils.decorators import mark_fault_injected


class LatentSectorErrorSaleor(Problem):
    """Blocks past the first page of the ``saleor`` database's relation files can no longer be read.

    The original stopped a MongoDB deployment, truncated its WiredTiger files
    to the 4 KiB header on the node and started it again, so reads of the
    missing blocks fail. Here the PostgreSQL StatefulSet is scaled to 0, every
    relation file of the ``saleor`` database (``base/<oid>/``, catalogs
    included) is truncated to its first 8 KiB page on the node, and the
    database is started again. Connections then fail with FATAL/PANIC
    ``could not open critical system index 2662`` and the server crash-loops,
    so every API request fails.

    A ``pg_dump`` taken just before (on the database's own volume, under
    ``backups/``) is the realistic way back; the harness also snapshots the
    data directory on the node so recovery restores it exactly.
    """

    STS = POSTGRES
    SNAPSHOT_ROOT = "/var/lib/sregym-snapshots"
    BACKUP_DIR = "/var/lib/postgresql/data/backups"

    def __init__(self, app_name: str = APP):
        self.faulty_service = self.STS
        configure_port(
            self,
            app_name,
            component=f"statefulset/{self.STS}",
            description=(
                f"The storage backing Saleor's PostgreSQL (`{self.STS}` StatefulSet, PVC `data-{self.STS}-0`) "
                "developed latent sector errors: the relation files of the `saleor` database on the Postgres "
                "volume (`pgdata/base/<database oid>/`, system catalogs and their indexes included) were truncated "
                "to their first 8 KiB page and its relcache file `pg_internal.init` is gone. Connections to the "
                "database fail with FATAL/PANIC errors such as `could not open critical system index 2662` "
                "(pg_class_oid_index), and the PostgreSQL pod crash-loops, so the Saleor API and worker cannot "
                "load anything and every storefront and checkout request fails. Naming truncation/corruption of the "
                "`saleor` database files on the Postgres volume (including naming the damaged system index) is a "
                "correct localization. The repair is restoring the data, e.g. by recreating the database from the "
                f"`pg_dump` archive in `{self.BACKUP_DIR}` on the database volume or restoring the volume; "
                "reseeding the database with fresh fixture data is not a repair."
            ),
            oracle_factory=SaleorFaultStateOracle,
        )

    def _volume_location(self) -> tuple[str, str]:
        core = self.kubectl.core_v1_api
        pvc = core.read_namespaced_persistent_volume_claim(f"data-{self.STS}-0", self.namespace)
        pv = core.read_persistent_volume(pvc.spec.volume_name)
        path = pv.spec.local.path if pv.spec.local else pv.spec.host_path.path
        node = None
        terms = pv.spec.node_affinity.required.node_selector_terms if pv.spec.node_affinity else []
        for term in terms or []:
            for expr in term.match_expressions or []:
                if expr.key == "kubernetes.io/hostname" and expr.values:
                    node = expr.values[0]
        if node is None:
            node = core.read_namespaced_pod(f"{self.STS}-0", self.namespace).spec.node_name
        return node, path

    def _scale(self, replicas: int) -> None:
        self.kubectl.exec_command_checked(
            f"kubectl scale statefulset/{self.STS} -n {self.namespace} --replicas={replicas}"
        )
        if replicas == 0:
            self.kubectl.exec_command(f"kubectl wait pod/{self.STS}-0 -n {self.namespace} --for=delete --timeout=180s")
        else:
            time.sleep(5)
            self.kubectl.exec_command(
                f"kubectl wait pod/{self.STS}-0 -n {self.namespace} --for=condition=Ready --timeout=300s"
            )

    def _node_script(self, node: str, script: str) -> str:
        ensure_namespace(self, HIDDEN_NAMESPACE)
        return self.kubectl.run_node_script_pod(
            node_name=node, namespace=HIDDEN_NAMESPACE, script=script, name_prefix="sregym-storage-fault", timeout=600
        )

    def _snapshot(self) -> str:
        return f"{self.SNAPSHOT_ROOT}/{self.namespace}-{self.STS}"

    def _orders(self) -> int:
        return int(self.app.psql("SELECT count(*) FROM order_order").strip())

    @mark_fault_injected
    def inject_fault(self):
        oid = self.app.psql(f"SELECT oid FROM pg_database WHERE datname = '{self.app.DATABASE}'").strip()
        orders = self._orders()
        stamp = time.strftime("%Y%m%d-%H%M")
        self.app.exec_in(
            f"pod/{self.STS}-0",
            f"mkdir -p {self.BACKUP_DIR} && PGPASSWORD={self.app.ADMIN_PASSWORD} pg_dump -h 127.0.0.1 "
            f"-U {self.app.ADMIN_ROLE} -Fc -f {self.BACKUP_DIR}/{self.app.DATABASE}-{stamp}.dump {self.app.DATABASE}",
            container="postgres",
            timeout=600,
        )
        node, path = self._volume_location()
        state_file(self, "latent-sector").write_text(json.dumps({"node": node, "path": path, "orders": orders}))
        self._scale(0)
        script = f"""set -e
cd "/host{path}"
rm -rf "/host{self._snapshot()}"; mkdir -p "/host{self._snapshot()}"
cp -a . "/host{self._snapshot()}/"
cd pgdata/base/{oid}
rm -f pg_internal.init
n=0
for f in *; do
    case "$f" in PG_VERSION|pg_filenode.map) continue;; esac
    [ -f "$f" ] || continue
    size=$(stat -c %s "$f")
    [ "$size" -gt 8192 ] || continue
    truncate -s 8192 "$f"; n=$((n+1))
done
echo "truncated $n relation files"
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        deadline = time.monotonic() + 120
        while True:
            try:
                self.app.psql("SELECT count(*) FROM order_order JOIN account_address a ON true", timeout=30)
            except Exception as exc:
                print(f"saleor database unreadable: {str(exc)[-200:]}")
                break
            if time.monotonic() > deadline:
                raise RuntimeError("the saleor database is still readable after truncation")
            time.sleep(5)
        print(f"Truncated the relation files of database {self.app.DATABASE} (oid {oid}) on {node}:{path}")

    @mark_fault_injected
    def recover_fault(self):
        saved = json.loads(state_file(self, "latent-sector").read_text())
        node, path = saved["node"], saved["path"]
        self._scale(0)
        script = f"""set -e
test -d "/host{self._snapshot()}/pgdata"
cd "/host{path}"
find . -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
cp -a "/host{self._snapshot()}/." .
rm -rf "/host{self._snapshot()}"
echo restored
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.STS} -n {self.namespace} --timeout=600s", timeout=630
        )
        # The API and worker hold broken pooled connections from the outage; recycle them.
        for deployment in (API, WORKER):
            self.app.rollout_restart("deployment", deployment)

    def fault_check(self, oracle) -> dict | None:
        sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.STS, self.namespace)
        if (sts.status.ready_replicas or 0) < 1:
            print(f"❌ {self.STS} is not ready")
            return oracle.fail("fault_still_present", statefulset=self.STS)
        try:
            listing = self.app.psql(
                "SELECT quote_ident(tablename) FROM pg_tables WHERE schemaname = 'public' ORDER BY 1", timeout=60
            )
        except Exception as exc:
            print(f"❌ Cannot list the tables of {self.app.DATABASE}: {str(exc)[-200:]}")
            return oracle.fail("fault_still_present", error=str(exc)[-300:])
        tables = [t.strip() for t in listing.splitlines() if t.strip()]
        unreadable = []
        for start in range(0, len(tables), 40):
            batch = tables[start : start + 40]
            try:
                self.app.psql("; ".join(f"SELECT count(*) FROM {t}" for t in batch), timeout=120)
            except Exception:
                for table in batch:
                    try:
                        self.app.psql(f"SELECT count(*) FROM {table}", timeout=60)
                    except Exception:
                        unreadable.append(table)
        if unreadable or not tables:
            print(f"❌ Unreadable tables in {self.app.DATABASE}: {unreadable[:10]} ({len(unreadable)} total)")
            return oracle.fail("fault_still_present", unreadable=unreadable[:20], tables=len(tables))
        path = state_file(self, "latent-sector")
        before = json.loads(path.read_text()).get("orders", 0) if path.exists() else 0
        if self._orders() < before:
            print(f"❌ Orders were lost: {before} before the fault, {self._orders()} now")
            return oracle.fail("fault_still_present", detail="orders lost", before=before, after=self._orders())
        print(f"✅ All {len(tables)} tables of {self.app.DATABASE} are readable")
        return require_checkout(self, oracle)
