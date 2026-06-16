"""CASSANDRA-14204: AssertionError in `nodetool garbagecollect` when a table has
`only_purge_repaired_tombstones=true` and a MIX of repaired + unrepaired sstables.

Title: Remove unrepaired SSTables from garbage collection when
       `only_purge_repaired_tombstones` is true (avoids AssertionError).
JIRA:  https://issues.apache.org/jira/browse/CASSANDRA-14204

Buggy: 4.1.1  ->  Fixed: 4.1.3 (also 3.11.16, 4.0.11, 5.0-alpha1, 5.0).
  (The bug was reproduced on stock cassandra:4.1.1 — a pre-fix release on the
   4.1 line with the IDENTICAL unfixed code path. The original candidate buggy
   tag 4.1.2 could not be pulled from Docker Hub, see the evidence log; 4.1.1 is
   < the 4.1.3 fix so it carries the bug. A/B-controlled against fixed 4.1.11.)

Reproduction summary (single node, local compaction):
  Create a table WITH compaction={'class':'SizeTieredCompactionStrategy',
  'only_purge_repaired_tombstones':'true'}. Build a MIX of one REPAIRED sstable
  and one UNREPAIRED sstable, then run `nodetool garbagecollect <ks> <table>`.
  With the flag on, filterSSTables() drops the unrepaired sstable from the
  returned set but it remains in the compaction transaction -> the size assertion
  in parallelAllSSTableOperation fails. (All-unrepaired does NOT fire it: it
  short-circuits with "No sstables to GARBAGE_COLLECT".)

Verbatim buggy signature (cassandra:4.1.1, `nodetool garbagecollect`, exit 2):

    error: null
    -- StackTrace --
    java.lang.AssertionError
        at org.apache.cassandra.db.compaction.CompactionManager.parallelAllSSTableOperation(CompactionManager.java:407)
        at org.apache.cassandra.db.compaction.CompactionManager.performGarbageCollection(CompactionManager.java:620)
        at org.apache.cassandra.db.ColumnFamilyStore.garbageCollect(ColumnFamilyStore.java:1720)
        at org.apache.cassandra.service.StorageService.garbageCollect(StorageService.java:3958)
        ...
    command terminated with exit code 2

Shape: nodetool/flush sequence (NOT pure CQL, NOT wrong-result). The repaired
sstable is minted via `nodetool repair` (incremental anticompaction on the
RF=3 K8ssandra cluster marks the batch-1 sstable repaired with no daemon
stop/offline tool). inject_fault() is overridden to run the full nodetool+CQL
sequence on a single Cassandra pod via `kubectl exec`. See inject_fault() for
the log-verified offline `sstablerepairedset` fallback.
"""

import logging
import re
import subprocess
import time

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Keyspace / table used by the reproducer.
_KS = "repro14204"
_TBL = "t"

# Step 1 — table with the bug-gating flag. RF=3 so the data lands on every pod
# regardless of token hashing AND so incremental `nodetool repair` actually runs
# (RF=1 single-node repair short-circuits with "No repair is needed", which is
# why the evidence log fell back to the offline sstablerepairedset tool).
_CREATE_CQL = (
    f"CREATE KEYSPACE IF NOT EXISTS {_KS} "
    f"WITH replication = {{'class':'SimpleStrategy','replication_factor':3}}; "
    f"CREATE TABLE IF NOT EXISTS {_KS}.{_TBL} (id int PRIMARY KEY, v text) "
    f"WITH compaction = {{'class':'SizeTieredCompactionStrategy',"
    f"'only_purge_repaired_tombstones':'true'}};"
)

# Step 2 — batch 1 (will be marked REPAIRED). Includes a DELETE so there is a
# tombstone for garbagecollect to act on.
_BATCH1_CQL = (
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (1,'a'); "
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (2,'b'); "
    f"DELETE FROM {_KS}.{_TBL} WHERE id=2;"
)

# Step 4 — batch 2 (stays UNREPAIRED) -> now a MIX of 1 repaired + 1 unrepaired.
_BATCH2_CQL = (
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (3,'c'); "
    f"INSERT INTO {_KS}.{_TBL}(id,v) VALUES (4,'d'); "
    f"DELETE FROM {_KS}.{_TBL} WHERE id=4;"
)


class AutoCassandra14204(GenericCustomBuildProblem):
    db_name = "cassandra"
    db_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    # 4.1.1 already ships the bug, so deploy the stock image instead of an
    # ant-jar build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/db/compaction/CompactionManager.java"
    root_cause_description = (
        "`nodetool garbagecollect` throws java.lang.AssertionError in "
        "CompactionManager.parallelAllSSTableOperation when the table has "
        "compaction option only_purge_repaired_tombstones=true and holds a mix of "
        "repaired and unrepaired sstables. CompactionManager$6.filterSSTables() "
        "removes the unrepaired sstables from the returned candidate set but they "
        "are NOT removed from the compaction transaction, so the size assertion "
        "(filtered.size() == transaction.originals().size()) in "
        "parallelAllSSTableOperation fails. The fix removes the unrepaired sstables "
        "from the GC transaction so the returned set matches the transaction."
    )

    # Authoritative buggy steps from the evidence log. inject_fault() executes
    # this sequence operationally (the bug needs nodetool flush/repair +
    # garbagecollect, which a single CQL string cannot express); this string is
    # the human-readable record of what is run and is also surfaced to oracles.
    reproducer = """
-- CASSANDRA-14204 reproducer (nodetool/flush sequence, run on ONE Cassandra pod)

-- 1. Table with the bug-gating flag (RF=3 so the partition exists on every pod
--    and so incremental `nodetool repair` actually anticompacts).
CREATE KEYSPACE IF NOT EXISTS repro14204
    WITH replication = {'class':'SimpleStrategy','replication_factor':3};
CREATE TABLE IF NOT EXISTS repro14204.t (id int PRIMARY KEY, v text)
    WITH compaction = {'class':'SizeTieredCompactionStrategy',
                       'only_purge_repaired_tombstones':'true'};

-- 2. Batch 1 (becomes the REPAIRED sstable) + flush.
INSERT INTO repro14204.t(id,v) VALUES (1,'a');
INSERT INTO repro14204.t(id,v) VALUES (2,'b');
DELETE FROM repro14204.t WHERE id=2;
-- nodetool flush repro14204 t

-- 3. Mark batch-1 sstable REPAIRED.
--    nodetool repair repro14204 t            (incremental; anticompacts -> repaired)
--    GATE: nodetool tablestats repro14204.t  -> Percent repaired > 0

-- 4. Batch 2 (stays UNREPAIRED) + flush  -> MIX: 1 repaired + 1 unrepaired.
INSERT INTO repro14204.t(id,v) VALUES (3,'c');
INSERT INTO repro14204.t(id,v) VALUES (4,'d');
DELETE FROM repro14204.t WHERE id=4;
-- nodetool flush repro14204 t
-- GATE: nodetool tablestats repro14204.t -> SSTable count: 2, 0 < Percent repaired < 100

-- 5. THE REPRODUCER -> java.lang.AssertionError in parallelAllSSTableOperation, exit 2.
-- nodetool garbagecollect repro14204 t
"""

    # Error/crash bug (AssertionError), NOT wrong-result -> leave expected_output
    # unset so the diagnosis oracle judges the root cause and no buggy-value grep
    # is installed.
    #
    # continuous_reproducer is False: this bug is a one-shot, stateful nodetool
    # sequence (build a specific repaired+unrepaired sstable mix, then fire one
    # garbagecollect). The shared continuous-reproducer probe loops a pure-CQL
    # `cqlsh < run.cql`, which cannot express the nodetool/flush/repair steps, so
    # enabling it would deploy a probe pod + mitigation oracle that never
    # reproduce this bug. Diagnosis-only, mirroring cassandra_20108.
    continuous_reproducer = False

    # ── Custom fault injection (nodetool/flush sequence) ───────────────────────

    def _server_pod(self) -> str | None:
        """Name of one Cassandra server pod in the deployed cluster."""
        out = (
            subprocess.run(
                f"kubectl get pods -n {self.namespace} "
                f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
                f"-o jsonpath='{{.items[0].metadata.name}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )
        return out or None

    def _exec(self, pod: str, inner: str, *, timeout: int = 180) -> subprocess.CompletedProcess:
        """Run a shell command inside the `cassandra` container of `pod`."""
        cmd = f"kubectl exec -n {self.namespace} {pod} -c cassandra -- bash -c {subprocess.list2cmdline([inner])}"
        cp = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        logger.info(
            "[AutoCassandra14204] exec rc=%s :: %s\n  stdout=%s\n  stderr=%s",
            cp.returncode,
            inner[:120],
            cp.stdout.strip()[:300],
            cp.stderr.strip()[:300],
        )
        return cp

    def _cqlsh(self, pod: str, cql: str, *, timeout: int = 180, retries: int = 8) -> subprocess.CompletedProcess:
        """Run a CQL string via cqlsh on the local pod (127.0.0.1).

        Waits for the pod's CQL native transport to be serving (management-api
        readiness=200) before each attempt and retries on transient connection errors:
        during the operator/StatefulSet rolling image swap a pod's port 9042 is briefly
        refused, which would otherwise drop the seed INSERTs and break the
        repaired/unrepaired SSTable mix the bug needs.
        """
        cp = subprocess.CompletedProcess(args="", returncode=1, stdout="", stderr="not run")
        for attempt in range(1, retries + 1):
            self._wait_pod_ready(pod)
            cp = subprocess.run(
                f"kubectl exec -i -n {self.namespace} {pod} -c cassandra -- cqlsh {self._cqlsh_auth_flags()}127.0.0.1",
                shell=True,
                input=cql,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            blob = cp.stdout + cp.stderr
            if cp.returncode == 0:
                break
            transient = any(
                s in blob for s in ("Unable to connect", "ConnectionRefused", "NoHostAvailable", "Connection error")
            )
            logger.info(
                "[AutoCassandra14204] cqlsh rc=%s (attempt %s/%s, transient=%s)",
                cp.returncode,
                attempt,
                retries,
                transient,
            )
            if not transient:
                break
            time.sleep(10)
        logger.info(
            "[AutoCassandra14204] cqlsh rc=%s :: %s\n  stdout=%s\n  stderr=%s",
            cp.returncode,
            cql.replace("\n", " ")[:120],
            cp.stdout.strip()[:300],
            cp.stderr.strip()[:300],
        )
        return cp

    # ── Ring-stabilization helpers (GAP FIX #22) ──────────────────────────────
    # The buggy-image swap is a rolling restart; `nodetool repair` exits rc=2
    # ("Repair job failed") while any replica is still DN or schema is in flux, so
    # no repaired SSTable forms and `garbagecollect` never arms the AssertionError.
    # Wait for the whole ring to return to UN (single schema version) and retry the
    # incremental repair until it actually marks SSTables repaired.

    def _cassandra_pods(self) -> list[str]:
        out = subprocess.run(
            f"kubectl get pods -n {self.namespace} "
            f"-l app.kubernetes.io/instance=cassandra-{self.app.cluster_name} "
            f"--no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout
        return [p.strip() for p in out.splitlines() if p.strip()]

    def _pod_cql_ready(self, pod: str) -> bool:
        """True when the pod's management-api readiness probe reports Cassandra is
        serving CQL (HTTP 200 over the local unix socket)."""
        cp = subprocess.run(
            f"kubectl exec -n {self.namespace} {pod} -c cassandra -- "
            f"curl -sf -o /dev/null --unix-socket /tmp/oss-mgmt.sock "
            f"http://localhost/api/v0/probes/readiness",
            shell=True,
            capture_output=True,
            text=True,
        )
        return cp.returncode == 0

    def _pod_image(self, pod: str) -> str:
        """Image of the pod's ``cassandra`` container."""
        return (
            subprocess.run(
                f"kubectl get pod {pod} -n {self.namespace} "
                f"-o jsonpath='{{.spec.containers[?(@.name==\"cassandra\")].image}}'",
                shell=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .strip("'")
        )

    def _wait_pod_ready(self, pod: str, *, timeout: int = 600) -> bool:
        """Poll until the pod is serving CQL (management-api readiness=200)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._pod_cql_ready(pod):
                return True
            time.sleep(10)
        logger.warning("[AutoCassandra14204] %s did NOT become CQL-ready within %ss", pod, timeout)
        return False

    def _wait_all_pods_ready(self, *, timeout: int = 900) -> bool:
        """Wait until every cluster pod serves CQL (readiness=200) AND runs the buggy
        image, i.e. the operator/StatefulSet rolling image swap is fully complete and no
        pod will be cycled mid-reproducer (which would refuse the seed INSERTs on 9042
        and prevent the repaired/unrepaired SSTable mix from forming)."""
        deadline = time.time() + timeout
        target = self._custom_image
        while time.time() < deadline:
            pods = self._cassandra_pods()
            if pods:
                ready = sum(1 for p in pods if self._pod_cql_ready(p))
                on_img = sum(1 for p in pods if self._pod_image(p) == target)
                if ready == len(pods) and on_img == len(pods):
                    logger.info(
                        "[AutoCassandra14204] all %s pods CQL-ready on buggy image %s",
                        len(pods),
                        target,
                    )
                    return True
                logger.info(
                    "[AutoCassandra14204] waiting for all pods CQL-ready on buggy image: ready=%s on_img=%s of %s",
                    ready,
                    on_img,
                    len(pods),
                )
            time.sleep(15)
        logger.warning("[AutoCassandra14204] not all pods CQL-ready on the buggy image within %ss", timeout)
        return False

    @staticmethod
    def _parse_ring(status_stdout: str) -> tuple[int, int]:
        """Return (num_UN, num_total) parsed from ``nodetool status`` output."""
        states = ("UN", "UJ", "UL", "UM", "DN", "DJ", "DL", "DM")
        total = un = 0
        for ln in status_stdout.splitlines():
            s = ln.strip()
            if len(s) >= 2 and s[:2] in states and (len(s) == 2 or s[2] == " "):
                total += 1
                if s.startswith("UN"):
                    un += 1
        return un, total

    def _single_schema(self, pod: str) -> bool:
        out = self._exec(pod, "nodetool describecluster 2>/dev/null").stdout
        uuids = re.findall(r"^\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):", out, re.M)
        return len(set(uuids)) <= 1 and len(uuids) >= 1

    def _wait_ring_un(self, *, timeout: int = 1200) -> bool:
        """Poll until every ring pod is Up/Normal (UN) with a single schema version."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            pods = self._cassandra_pods()
            if not pods:
                time.sleep(10)
                continue
            probe = pods[0]
            un, total = self._parse_ring(self._exec(probe, "nodetool status 2>/dev/null").stdout)
            single = self._single_schema(probe) if (total > 0 and un == total) else False
            if total >= len(pods) and un == total and total > 0 and single:
                logger.info("[AutoCassandra14204] ring stable: %s/%s UN, single schema version", un, total)
                return True
            logger.info(
                "[AutoCassandra14204] waiting for ring all-UN: %s/%s UN, pods=%s, single_schema=%s",
                un,
                total,
                len(pods),
                single,
            )
            time.sleep(15)
        logger.warning("[AutoCassandra14204] ring did NOT reach all-UN within %ss", timeout)
        return False

    def _percent_repaired(self, pod: str) -> float:
        """Parse ``Percent repaired`` for the reproducer table from nodetool tablestats."""
        out = self._exec(
            pod, f"nodetool tablestats {_KS}.{_TBL} 2>/dev/null | grep -i 'percent repaired' || true"
        ).stdout
        m = re.search(r"[Pp]ercent repaired:\s*([0-9.]+)", out)
        return float(m.group(1)) if m else -1.0

    def _repair_until_repaired(self, pod: str, *, attempts: int = 5) -> bool:
        """Run incremental ``nodetool repair`` (retrying) until the batch-1 SSTable is
        marked repaired (Percent repaired > 0). Returns True on success."""
        for i in range(1, attempts + 1):
            self._wait_ring_un()
            cp = self._exec(pod, f"nodetool repair {_KS} {_TBL}", timeout=600)
            pr = self._percent_repaired(pod)
            logger.info(
                "[AutoCassandra14204] repair attempt %s/%s rc=%s -> Percent repaired=%s",
                i,
                attempts,
                cp.returncode,
                pr,
            )
            if pr > 0:
                logger.info("[AutoCassandra14204] repaired SSTable minted (Percent repaired=%.1f)", pr)
                return True
            time.sleep(20)
        logger.warning(
            "[AutoCassandra14204] nodetool repair did not mark SSTables repaired after %s attempts", attempts
        )
        return False

    @mark_fault_injected
    def inject_fault(self):
        """Swap in the buggy image, then drive the CASSANDRA-14204 nodetool
        sequence on a single Cassandra pod so `nodetool garbagecollect` throws
        the AssertionError (visible to the operator and in the system log).

        Sequence (authoritative log steps, mechanism adapted to the K8ssandra
        deploy — see module docstring):
          1. cqlsh: CREATE KEYSPACE (RF=3) + table WITH only_purge_repaired_tombstones=true
          2. cqlsh: INSERT batch 1 (+ DELETE)         ;  nodetool flush
          3. nodetool repair  -> batch-1 sstable becomes REPAIRED (anticompaction)
          4. cqlsh: INSERT batch 2 (+ DELETE)         ;  nodetool flush   (MIX now)
          5. nodetool garbagecollect  -> java.lang.AssertionError, exit 2  (THE BUG)

        Fallback if `nodetool repair` does not mark the sstable repaired in this
        environment: stop Cassandra (mgmt-api lifecycle / `nodetool stopdaemon`),
        run `sstablerepairedset --really-set --is-repaired
        /var/lib/cassandra/data/repro14204/t-*/*-Data.db`, then restart. The
        K8ssandra pod has a PVC, so the repairedAt flag survives a container
        restart (the evidence log only needed the tail-as-PID1 hack because the
        ad-hoc stock pod had NO PVC).
        """
        # Swap the running cluster to the buggy image first (4.1.1 is the buggy
        # build; if it was pre-deployed the base class no-ops the swap).
        if getattr(self, "_predeployed_buggy", False):
            logger.info("[AutoCassandra14204] Buggy image already deployed — skipping swap")
        else:
            logger.info("[AutoCassandra14204] Swapping cluster to buggy image %s", self._custom_image)
            self.app.inject_buggy_image(self._custom_image)

        self.setup_preconditions()

        pod = self._server_pod()
        if not pod:
            logger.warning("[AutoCassandra14204] No Cassandra server pod found — cannot run reproducer")
            return
        logger.info("[AutoCassandra14204] Driving reproducer on pod %s", pod)

        # GAP FIX (#6 + #22): the buggy-image swap is an operator-driven rolling
        # restart that scales BOTH operators to 0 and does not recreate the
        # rolling-restarted pod, so the ring is stuck at 2/3 (a replica permanently
        # DN). Scale the operators back to 1 so the missing pod is recreated, THEN
        # wait for the whole ring to be UN — otherwise `nodetool repair` exits rc=2
        # ("Repair job failed") forever, no repaired SSTable forms, and
        # `garbagecollect` never arms the AssertionError.
        self.app._scale_operator_up()
        self._wait_ring_un()
        # GAP FIX (CQL-readiness race): wait until the rolling image swap is FULLY done
        # (every pod on the buggy image and serving CQL) before any data step. Otherwise
        # the StatefulSet rolling update can still be cycling the target pod, the seed
        # INSERTs hit a briefly-refused port 9042, and the repaired/unrepaired SSTable
        # mix never forms (so garbagecollect short-circuits with no AssertionError).
        self._wait_all_pods_ready()

        # 1. schema with the bug-gating compaction flag (RF=3) and let it propagate.
        self._cqlsh(pod, _CREATE_CQL)
        self._wait_ring_un()

        # 2. batch 1 + flush -> one on-disk SSTable to be marked repaired.
        self._cqlsh(pod, _BATCH1_CQL)
        self._exec(pod, f"nodetool flush {_KS} {_TBL}")

        # 3. mark the batch-1 SSTable REPAIRED via incremental repair, retrying on
        #    the now-stable ring until anticompaction actually sets repairedAt.
        repaired = self._repair_until_repaired(pod)
        self._exec(pod, f"nodetool tablestats {_KS}.{_TBL} | grep -i 'percent repaired' || true")
        if not repaired:
            logger.warning(
                "[AutoCassandra14204] repair never marked SSTables repaired — "
                "garbagecollect AssertionError may not arm (no repaired/unrepaired mix)"
            )

        # 4. batch 2 (stays UNREPAIRED) + flush -> MIX of 1 repaired + 1 unrepaired.
        self._cqlsh(pod, _BATCH2_CQL)
        self._exec(pod, f"nodetool flush {_KS} {_TBL}")
        self._exec(pod, f"nodetool tablestats {_KS}.{_TBL} | grep -iE 'sstable count|percent repaired' || true")

        # 5. THE REPRODUCER — expected to fail with AssertionError (exit 2).
        gc = self._exec(pod, f"nodetool garbagecollect {_KS} {_TBL}")
        blob = gc.stdout + gc.stderr
        if gc.returncode != 0 and "AssertionError" in blob:
            logger.info(
                "[AutoCassandra14204] Reproduced CASSANDRA-14204: java.lang.AssertionError in "
                "garbagecollect (parallelAllSSTableOperation). VERBATIM SIGNATURE (rc=%s):\n%s",
                gc.returncode,
                blob.strip(),
            )
        else:
            logger.warning(
                "[AutoCassandra14204] garbagecollect rc=%s did not show the expected "
                "AssertionError (check repaired-sstable mix):\n%s",
                gc.returncode,
                blob.strip(),
            )
        # Scrape the server log for the verbatim stack frame regardless (the
        # AssertionError is logged server-side even when nodetool prints little).
        self._exec(
            pod,
            "grep -B2 -A14 -iE 'AssertionError' /var/log/cassandra/system.log | tail -60 || true",
        )

    @mark_fault_injected
    def recover_fault(self):
        """Restore the stock image and wait for the cluster to be Ready."""
        logger.info("[AutoCassandra14204] Recovering: restoring cluster to stock image")
        self.app.restore_stock_image(custom_image=self._custom_image)
        logger.info("[AutoCassandra14204] Recovery complete")
