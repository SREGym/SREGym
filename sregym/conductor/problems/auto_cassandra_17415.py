"""CASSANDRA-17415: dropping a materialized view does not create a snapshot with the 'dropped-' prefix.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-17415
Buggy: cassandra 3.11.12   ->   Fixed: cassandra 3.11.13 (3.11.x-only; refactored away in 4.x).

Reproduction summary (single 3.11.12 node, stock auto_snapshot: true):
  Create a base table and a materialized view over it, INSERT rows, then `nodetool flush`
  so both base-* and mv-* SSTables exist on disk. DROP the materialized view, then DROP the
  base table -- auto_snapshot fires a drop-time snapshot on each. The normal table's snapshot
  directory is correctly named with the `dropped-` prefix, but the MV's snapshot directory is
  named WITHOUT the `dropped-` prefix (just `<timestamp>-mv`). Snapshot-listing/recovery tooling
  keys on `dropped-` to identify drop-time snapshots, so the MV's snapshot is mislabeled.

Root cause (verified against the cassandra-3.11.x source tree, src/java/org/apache/cassandra/config/Schema.java):
  `dropColumnFamily()` (normal table drop) snapshots via
      cfs.snapshot(Keyspace.getTimestampedSnapshotNameWithPrefix(cfs.name, ColumnFamilyStore.SNAPSHOT_DROP_PREFIX))
  i.e. with the "dropped" prefix, whereas `dropView()` snapshots via
      cfs.snapshot(Keyspace.getTimestampedSnapshotName(cfs.name))
  i.e. WITHOUT the prefix. The fix (3.11.13) makes dropView() use the prefixed name too.

This bug produces NO cqlsh/server error -- the DROPs succeed. The observable is the snapshot
directory NAME on the server's data disk. Verbatim buggy signature (filesystem line, 3.11.12):

  /var/lib/cassandra/data/repro17415_ks/mv-e1917d60661911f1a8c4edaf56a013df/snapshots/1781239738805-mv

(MV snapshot directory lacks the `dropped-` prefix; the sibling normal-table snapshot on the same
node is `dropped-1781239759581-base`. The fixed 3.11.13+ build names the MV snapshot
`dropped-<timestamp>-mv`.)

Shape: nodetool/flush sequence with a FILESYSTEM observable. The standard CQL-only reproducer
infrastructure (a separate cqlsh client pod) cannot run `nodetool flush` on the server nor inspect
the server's data directory, so inject_fault() is overridden to drive cqlsh + nodetool + a `find`
directly inside the Cassandra server pod via `kubectl exec`. continuous_reproducer is False
(diagnosis-only): the ReproducerPodMitigationOracle probe only greps cqlsh stdout / checks cqlsh
exit code, and this bug surfaces neither -- so attaching a mitigation oracle would silently check
the wrong thing. There is therefore no expected_output (the buggy "value" is a server filesystem
path, invisible to the CQL probe).
"""

import base64 as _b64
import logging
import shlex
import subprocess

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_KS = "repro17415_ks"

# CREATE base + MV and INSERT rows so writes flow base -> MV.
# NOTE on replication_factor: the evidence log reproduced on a SINGLE-node cluster with RF=1
# (that one node owns every row, so it holds both the base-* and mv-* SSTables/snapshots). The
# K8ssandra manifest here deploys a 3-node datacenter (size: 3), so RF=3 is the 3-node analog: it
# keeps EVERY node authoritative for every row, guaranteeing the single server pod we inspect holds
# both the base and the MV SSTables -- otherwise (RF=1 on 3 nodes) the base and MV snapshots could
# land on different pods and the base-vs-mv `dropped-` contrast would not be visible on one pod.
_SETUP_CQL = f"""
CREATE KEYSPACE IF NOT EXISTS {_KS} WITH replication = {{'class':'SimpleStrategy','replication_factor':3}};
CREATE TABLE IF NOT EXISTS {_KS}.base (id int PRIMARY KEY, val text);
CREATE MATERIALIZED VIEW IF NOT EXISTS {_KS}.mv AS
  SELECT id, val FROM {_KS}.base WHERE id IS NOT NULL AND val IS NOT NULL
  PRIMARY KEY (val, id);
INSERT INTO {_KS}.base (id, val) VALUES (1, 'a');
INSERT INTO {_KS}.base (id, val) VALUES (2, 'b');
INSERT INTO {_KS}.base (id, val) VALUES (3, 'c');
"""

# Auto-snapshot fires on each drop (auto_snapshot: true is the stock default).
# DROP the MV first, then the base table.
_DROP_CQL = f"""
DROP MATERIALIZED VIEW {_KS}.mv;
DROP TABLE {_KS}.base;
"""


class AutoCassandra17415(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "3.11.12"
    source_git_ref = "cassandra-3.11.12"
    # 3.11.12 already ships the bug (fix is 3.11.13), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/config/Schema.java"
    root_cause_description = (
        "Dropping a materialized view does not create a drop-time snapshot with the 'dropped-' prefix "
        "(3.11.x only). In Schema.java, dropColumnFamily() (normal table drop) snapshots via "
        "Keyspace.getTimestampedSnapshotNameWithPrefix(cfs.name, ColumnFamilyStore.SNAPSHOT_DROP_PREFIX) "
        "-- i.e. with the 'dropped' prefix -- whereas dropView() snapshots via "
        "Keyspace.getTimestampedSnapshotName(cfs.name), omitting the prefix. As a result the MV's "
        "snapshot directory is named '<timestamp>-mv' instead of 'dropped-<timestamp>-mv', so "
        "snapshot-listing/recovery tooling that keys on the 'dropped-' prefix mislabels it. The fix "
        "(3.11.13) makes dropView() use the prefixed name, matching the normal table-drop path."
    )

    # Full buggy steps, for the record / for the agent-under-test. This is NOT run by the
    # standard CQL-only run_reproducer path (it includes a `nodetool flush` and a filesystem
    # `find`, neither expressible as cqlsh-only). inject_fault() below drives it on the server pod.
    reproducer = f"""
-- Run ON a single Cassandra 3.11.12 server pod (auto_snapshot: true is the stock default):
{_SETUP_CQL.strip()}

-- Flush so both base-* and mv-* SSTables exist on disk before the drops:
nodetool flush {_KS}
-- (these images need JVM_OPTS="-Dcom.sun.jndi.rmiURLParsing=legacy" for nodetool JMX to connect)

-- confirm SSTables for BOTH base-* and mv-* exist:
find /var/lib/cassandra/data/{_KS} -type f -name 'me-*-Data.db'

{_DROP_CQL.strip()}

-- OBSERVABLE: the MV's snapshot directory lacks the 'dropped-' prefix that the normal table has:
find /var/lib/cassandra/data/{_KS} -type d -path '*/snapshots/*'
-- BUGGY 3.11.12 ->  base: .../snapshots/dropped-<ts>-base   (has 'dropped-')
--                   mv:   .../snapshots/<ts>-mv             (MISSING 'dropped-')   <-- THE BUG
-- FIXED 3.11.13 ->  mv:   .../snapshots/dropped-<ts>-mv     (has 'dropped-')
""".strip()

    # Diagnosis-only: see module docstring -- the CQL-stdout/exit-code mitigation probe
    # cannot observe a server-filesystem snapshot-directory name.
    continuous_reproducer = False

    # ── Server-pod helpers ───────────────────────────────────────────────────

    def _server_pod(self) -> str | None:
        """Name of a Cassandra *server* pod (not the CQL client pod).

        The K8ssandra cluster pods carry app.kubernetes.io/instance=<cluster_name>;
        the data directory /var/lib/cassandra/data lives on these pods, so the
        nodetool/flush + `find` must run here.
        """
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        pods = [p.strip() for p in out.splitlines() if p.strip()]
        return pods[0] if pods else None

    def _cql_credentials(self) -> tuple[str, str]:
        """Read the K8ssandra-managed superuser secret (<cluster_name>-superuser)."""
        secret = f"{self.app.cluster_name}-superuser"

        def _field(key: str) -> str:
            val = (
                subprocess.run(
                    f"kubectl get secret {secret} -n {self.namespace} -o jsonpath='{{.data.{key}}}'",
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .strip("'")
            )
            return _b64.b64decode(val).decode() if val else ""

        return _field("username"), _field("password")

    def _exec_on_pod(self, pod: str, inner_sh: str, timeout: int = 180) -> str:
        """Run a /bin/sh snippet inside the cassandra container of a server pod."""
        cmd = f"kubectl exec -n {self.namespace} {pod} -c cassandra -- sh -c {shlex.quote(inner_sh)}"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (res.stdout or "") + (res.stderr or "")

    # ── Fault injection ────────────────────────────────────────────────────────

    @mark_fault_injected
    def inject_fault(self):
        """Drive the snapshot-naming reproducer on a Cassandra server pod.

        Because prebuilt_from_stock=True and db_version=3.11.12 (the buggy release),
        the deployed stock cluster is already buggy; the image swap is effectively a
        no-op kept only for parity with the GenericCustomBuildProblem lifecycle.
        """
        if not self._predeployed_buggy:
            logger.info(f"[Cassandra17415] Swapping cluster to buggy image: {self._custom_image}")
            try:
                self.app.inject_buggy_image(self._custom_image)
            except Exception as e:
                logger.warning(f"[Cassandra17415] inject_buggy_image raised (non-fatal here): {e}")
        else:
            logger.info("[Cassandra17415] Buggy stock image (3.11.12) already deployed — no swap needed")

        pod = self._server_pod()
        if not pod:
            logger.warning("[Cassandra17415] No Cassandra server pod found — cannot run reproducer")
            return
        logger.info(f"[Cassandra17415] Driving reproducer on server pod {pod}")

        user, pw = self._cql_credentials()
        # cqlsh against localhost on the server pod; auth flags only if creds exist.
        auth = f"-u {shlex.quote(user)} -p {shlex.quote(pw)} " if user else ""
        cqlsh = f"cqlsh {auth}127.0.0.1"

        def _cqlsh_heredoc(cql: str) -> str:
            return f"{cqlsh} <<'CQLEOF'\n{cql.strip()}\nCQLEOF\n"

        # 1) CREATE base + MV and INSERT rows.
        logger.info("[Cassandra17415] Creating base table + materialized view and inserting rows")
        out = self._exec_on_pod(pod, _cqlsh_heredoc(_SETUP_CQL))
        logger.info(f"[Cassandra17415] setup cqlsh output: {out.strip()[:300]}")

        # 2) Flush so base-* and mv-* SSTables exist on disk before the drops.
        #    nodetool on these images needs the legacy RMI URL parser to connect via JMX.
        logger.info(f"[Cassandra17415] nodetool flush {_KS}")
        out = self._exec_on_pod(
            pod,
            f'JVM_OPTS="-Dcom.sun.jndi.rmiURLParsing=legacy" nodetool flush {_KS}',
        )
        logger.info(f"[Cassandra17415] nodetool flush output: {out.strip()[:300]}")

        sstables = self._exec_on_pod(pod, f"find /var/lib/cassandra/data/{_KS} -type f -name 'me-*-Data.db'")
        logger.info(f"[Cassandra17415] SSTables on disk before drop:\n{sstables.strip()}")

        # 3) DROP the MV then the base table — auto_snapshot fires on each.
        logger.info("[Cassandra17415] Dropping materialized view then base table (auto_snapshot fires)")
        out = self._exec_on_pod(pod, _cqlsh_heredoc(_DROP_CQL))
        logger.info(f"[Cassandra17415] drop cqlsh output: {out.strip()[:300]}")

        # 4) OBSERVABLE: surface the snapshot directory names. The MV's lacks 'dropped-'.
        snaps = self._exec_on_pod(
            pod,
            f"find /var/lib/cassandra/data/{_KS} -type d -path '*/snapshots/*' | sort",
        )
        logger.info(
            "[Cassandra17415] Snapshot directories on disk (BUG: the mv-* snapshot lacks the "
            "'dropped-' prefix that the base-* snapshot has):\n%s",
            snaps.strip(),
        )
        if "/snapshots/dropped-" in snaps and "-mv" in snaps:
            mv_lines = [ln for ln in snaps.splitlines() if "-mv" in ln]
            if any("/snapshots/dropped-" not in ln for ln in mv_lines):
                logger.info(
                    "[Cassandra17415] Confirmed CASSANDRA-17415: MV snapshot directory is missing "
                    "the 'dropped-' prefix while the base-table snapshot has it."
                )
