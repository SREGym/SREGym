"""Frappe: storage."""

from __future__ import annotations

import json

from sregym.conductor.oracles.apps.frappe.state import FrappeFaultStateOracle
from sregym.conductor.problems.apps.frappe.helpers import APP, GUNICORN, site_db_probe, state_file
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.conductor.problems.mongo_storage_faults import FAULT_POD_NAMESPACE
from sregym.service.apps.frappe.application import Frappe
from sregym.service.apps.frappe.operations import site_database
from sregym.utils.decorators import mark_fault_injected


class SilentDataCorruptionFrappe(Problem):
    """Overwrite InnoDB pages of Frappe's MariaDB volume with random bytes.

    As in the MongoDB original, the database is stopped, its files are damaged
    on the node (keeping each file's first page so the files look intact), and
    it is started again: InnoDB fails page checksums and mysqld aborts on every
    start. A ``bench backup`` taken just before (in the site's backups folder)
    is the realistic way back; the harness also snapshots the data directory on
    the node so recovery can restore it without reinstalling the site.
    """

    STS = Frappe.MARIADB_STATEFULSET
    SNAPSHOT_ROOT = "/var/lib/sregym-snapshots"

    def __init__(self, app_name: str = APP):
        super().__init__(app=make_app(app_name))
        self.faulty_service = self.STS
        configure_port(
            self,
            app_name,
            component=f"statefulset/{self.STS}",
            description=(
                f"The data files on the persistent volume backing Frappe's MariaDB (`{self.STS}`) were silently "
                "corrupted at the storage layer, without any I/O errors being reported: InnoDB pages of the system "
                "tablespace (`ibdata1`) and of the site schema's `.ibd` files contain garbage. InnoDB detects the "
                "damage as page checksum failures when it reads them and mysqld aborts during startup on every "
                "start, so the database is in CrashLoopBackOff and every request of the Frappe site fails. The data "
                "must be restored (e.g. reinitialize the database and restore the site's latest `bench backup` from "
                "`sites/svc-frappe-web/private/backups`)."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    def _volume_location(self) -> tuple[str, str]:
        core = self.kubectl.core_v1_api
        claim = f"data-{self.STS}-0"
        pvc = core.read_namespaced_persistent_volume_claim(claim, self.namespace)
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

    def _node_script(self, node: str, script: str) -> str:
        self.kubectl.create_namespace_if_not_exist(FAULT_POD_NAMESPACE)
        return self.kubectl.run_node_script_pod(
            node_name=node,
            namespace=FAULT_POD_NAMESPACE,
            script=script,
            name_prefix="sregym-storage-fault",
            timeout=600,
        )

    @mark_fault_injected
    def inject_fault(self):
        database = site_database(self.app)
        try:
            out = self.app.exec_in(
                f"deploy/{GUNICORN}",
                f"cd /home/frappe/frappe-bench && bench --site {self.app.SITE_NAME} backup",
                timeout=600,
            )
            print(out.strip().splitlines()[-1] if out.strip() else "bench backup done")
        except Exception as exc:  # The fault does not depend on it.
            print(f"⚠️ bench backup failed: {exc}")
        node, path = self._volume_location()
        snapshot = f"{self.SNAPSHOT_ROOT}/{self.namespace}-{self.STS}"
        state_file(self, "silent-corruption").write_text(json.dumps({"node": node, "path": path}))
        self._scale(0)
        script = f"""set -e
cd "/host{path}"
rm -rf "/host{snapshot}"; mkdir -p "/host{snapshot}"
cp -a . "/host{snapshot}/"
cd data
for f in ibdata1 {database}/*.ibd; do
    size=$(stat -c %s "$f")
    [ "$size" -gt 32768 ] || continue
    dd if=/dev/urandom of="$f" bs=16384 seek=1 count=$((size / 16384 - 1)) conv=notrunc 2>/dev/null
done
echo corrupted
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        print(f"Corrupted InnoDB pages of {self.STS} on {node}:{path}")

    @mark_fault_injected
    def recover_fault(self):
        saved = json.loads(state_file(self, "silent-corruption").read_text())
        node, path = saved["node"], saved["path"]
        snapshot = f"{self.SNAPSHOT_ROOT}/{self.namespace}-{self.STS}"
        self._scale(0)
        script = f"""set -e
test -d "/host{snapshot}/data"
cd "/host{path}"
find . -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
cp -a "/host{snapshot}/." .
rm -rf "/host{snapshot}"
echo restored
"""
        print(self._node_script(node, script).strip())
        self._scale(1)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.STS} -n {self.namespace} --timeout=600s", timeout=630
        )

    def fault_check(self, oracle) -> dict | None:
        sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.STS, self.namespace)
        if (sts.status.ready_replicas or 0) < 1:
            print(f"❌ {self.STS} is not ready")
            return oracle.fail("fault_still_present", statefulset=self.STS)
        return site_db_probe(self, oracle)
