"""CASSANDRA-20877 — FINALIZED incremental-repair sessions not cleaned up after range movement.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-20877
Buggy: 4.0.19  ->  Fixed: 4.0.20 (A/B control = cassandra:4.0.20).
Component: Consistency/Repair.

THE BUG (a real 3-node ring grown from 2 nodes so a set of ranges MOVES):
  ``system.repairs`` is local per node and pruned by ``LocalSessions#cleanup()`` every
  ``cassandra.repair_cleanup_interval_seconds``. It deletes FINALIZED sessions older than
  ``cassandra.repair_delete_timeout_seconds`` ONLY IF ``LocalSessions#isSuperseded(session)`` is true —
  i.e. every range+table the session covered has since been re-repaired by a newer session. After a node
  bootstraps, a set of ranges moves off the old nodes; those moved ranges are no longer re-repaired on the
  old nodes, so the last pre-movement FINALIZED session is never superseded and its row is kept FOREVER.
  On 4.0.19 ``isSuperseded`` returns false for such a session, so ``cleanup()`` logs
  ``Skipping delete of FINALIZED LocalSession <id> because it has not been superseded ...`` every interval,
  indefinitely. The fix (4.0.20) makes the corrected logic ignore ranges no longer owned, so the
  pre-movement session is auto-deleted.

  The discriminating node is the S2 REPAIR COORDINATOR (cass-0): a default ``nodetool repair`` there
  advances ``repairedAt`` for every range cass-0 still replicates, so the ONLY thing that can leave the
  pre-movement session not-superseded on cass-0 is the ranges that moved to cass-2. (cass-1 retains the
  pre-movement session on BOTH versions — it is not the S2 coordinator — so it does not discriminate.)

VERBATIM BUGGY SIGNATURE (cass-0 ``/var/log/cassandra/debug.log``, recurs every cleanup interval forever):
  Skipping delete of FINALIZED LocalSession <uuid> because it has not been superseded by a more recent session

Fixed 4.0.20 instead emits, for the same pre-movement session, an ``Auto deleting repair session
LocalSession{...}`` line and removes its ``system.repairs`` row — so cass-0 ends with the post-movement
session only (1 FINALIZED row) while 4.0.19 keeps both (2 FINALIZED rows) indefinitely.

HOW THE RAW-RING HARNESS MAKES THIS RUNNABLE (the former blockers, resolved):
  * The 1-day delete timeout / 10-min cleanup interval are shrunk to 30 s / 20 s via ``jvm_extra_opts``
    (``-Dcassandra.repair_delete_timeout_seconds=30 -Dcassandra.repair_cleanup_interval_seconds=20``) so the
    cleanup verdict is observable in budget.
  * Range movement — ``replicas=0`` + bare pods: ``post_deploy`` brings up a 2-node ring (cass-0 self-seed,
    cass-1 seeded off cass-0's IP); ``inject_fault`` runs the pre-movement repair S1 on cass-0, then launches
    a THIRD bootstrapping node (cass-2, seeded off cass-0) which takes over ~1/3 of the ranges (the movement),
    then runs S2 on cass-0. Bare pods are used (not the StatefulSet) so each node can write a
    ``/etc/profile.d`` PATH shim — on the 4.0.19 stock image a login shell (``bash -lc``, which the harness'
    ``exec`` uses) does NOT include ``/opt/cassandra/bin``, so nodetool/cqlsh would otherwise be unreachable.
    The three bare pods are named ``cass-seed`` (= "cass-0" in the evidence; the S2 coordinator and the
    discriminating node), ``cass-peer`` (= "cass-1") and ``cass-join`` (= "cass-2", the bootstrapping node);
    non-ordinal names are required so the (replicas=0) StatefulSet controller does not adopt-and-delete them.
  * Detection — the persistent, deterministic discriminator is cass-0 retaining the pre-movement FINALIZED
    session: ``CassandraLogGrepOracle(source='command')`` runs a command on cass-0 that counts FINALIZED
    (state=4) ``system.repairs`` rows and, when >= 2 remain, emits ``BUG20877_FINALIZED_RETAINED=<n>`` with
    the verbatim ``Skipping delete ...`` line appended. (A bare log grep is NOT a clean discriminator: the
    newest session is legitimately "skipped" on the fixed binary too — only the RETAINED pre-movement row on
    the S2-coordinator distinguishes 4.0.19 from 4.0.20.)

Verified end-to-end on kind-fleet3: after S1 -> bootstrap cass-2 -> S2, cass-0's debug.log emits
``Skipping delete of FINALIZED LocalSession <id> because it has not been superseded by a more recent
session`` every 20 s and cass-0 keeps 2 FINALIZED ``system.repairs`` rows (the A/B-documented fixed 4.0.20
deletes the pre-movement row, leaving 1).
"""

import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra20877(CassandraRawRingProblem):
    """3-node ring grown from 2; cass-0 keeps a pre-movement FINALIZED repair session forever (4.0.19)."""

    db_name = "cassandra"
    db_version = "4.0.19"
    cassandra_version = "4.0.19"
    source_git_ref = "cassandra-4.0.19"
    ring_namespace = "cassraw-20877"
    # No StatefulSet ring: bare pods so each node can write a /etc/profile.d PATH shim (the 4.0.19
    # stock image's login shell drops /opt/cassandra/bin) and so seeds/bootstrap are controlled by hand.
    replicas = 0
    # Make the 1-day delete timeout / 10-min cleanup interval observable in budget.
    jvm_extra_opts = "-Dcassandra.repair_delete_timeout_seconds=30 -Dcassandra.repair_cleanup_interval_seconds=20"

    root_cause_file = "src/java/org/apache/cassandra/repair/consistent/LocalSessions.java"
    root_cause_description = (
        "FINALIZED incremental-repair sessions are not cleaned up after range movement. "
        "LocalSessions.cleanup() deletes a FINALIZED session older than "
        "cassandra.repair_delete_timeout_seconds only if LocalSessions.isSuperseded(session) is true — "
        "every range+table it covered must have been re-repaired by a newer session. After a node "
        "bootstraps, a set of ranges moves off the old nodes and is no longer re-repaired there, so the "
        "last pre-movement session is never superseded; on 4.0.19 isSuperseded returns false and cleanup() "
        "logs `Skipping delete of FINALIZED LocalSession <id> because it has not been superseded by a more "
        "recent session` every interval, keeping the system.repairs row forever. The discriminating node is "
        "the S2 repair coordinator (cass-0): a default `nodetool repair` there advances repairedAt for every "
        "range cass-0 still replicates, so only the moved-away ranges leave the pre-movement session "
        "not-superseded. The fix (4.0.20) ignores ranges no longer owned and auto-deletes the session. "
        "Component: Consistency/Repair."
    )

    _SEED = "cass-seed"
    _PEER = "cass-peer"
    _JOINER = "cass-join"
    _KS = "ks20877"
    _TABLE = "ks20877.t"
    # Sentinel emitted by the oracle command only when cass-0 retains >= 2 FINALIZED repair sessions
    # (the pre-movement S1 + post-movement S2). A fixed 4.0.20 deletes the pre-movement row -> 1 -> no match.
    _BUGGY_PATTERN = r"BUG20877_FINALIZED_RETAINED=[2-9]"
    _SKIP_LINE = "Skipping delete of FINALIZED LocalSession"
    # cass-0 retained-FINALIZED-session count + verbatim skip line, as one in-pod command.
    _COUNT_CMD = (
        'n=$(cqlsh -e "SELECT parent_id,state FROM system.repairs" 2>/dev/null | '
        'grep -cE "\\|[[:space:]]+4[[:space:]]*$"); '
        'skip=$(grep "Skipping delete of FINALIZED LocalSession" /var/log/cassandra/debug.log | tail -1); '
        'if [ "${n:-0}" -ge 2 ]; then echo "BUG20877_FINALIZED_RETAINED=$n :: $skip"; '
        'else echo "ok finalized_retained=$n"; fi'
    )

    # ── ring node command (PATH shim so bash -lc finds nodetool/cqlsh on 4.0.19) ──
    def _node_cmd(self) -> str:
        return (
            "echo 'export PATH=$PATH:/opt/cassandra/bin:/opt/java/openjdk/bin' > /etc/profile.d/cass.sh\n"
            "sed -ri 's/^(hinted_handoff_enabled:).*/\\1 false/' /etc/cassandra/cassandra.yaml || true\n"
            "exec docker-entrypoint.sh cassandra -f\n"
        )

    def post_deploy(self):
        """Stand up the initial 2-node ring (cass-seed + cass-peer) and seed the keyspace/table."""
        app = self.app
        logger.info("[20877] post_deploy: standing up initial 2-node ring")
        app.apply_bare_pod(self._SEED, command=self._node_cmd(), env={"CASSANDRA_RACK": "rack1"}, set_seeds=False)
        app.wait_pod_running(self._SEED, timeout=300)
        seed_ip = app.pod_ip(self._SEED)
        logger.info(f"[20877] seed IP={seed_ip}")
        app.apply_bare_pod(
            self._PEER,
            command=self._node_cmd(),
            env={"CASSANDRA_RACK": "rack1", "CASSANDRA_SEEDS": seed_ip},
        )
        app.wait_pod_running(self._PEER, timeout=300)
        if not app.wait_ring(2, observer_pod=self._SEED, timeout=600):
            logger.warning("[20877] post_deploy: 2-node ring did not reach 2x UN within 600s")
        self._seed_ip = seed_ip
        self._create_schema()

    def _create_schema(self):
        app = self.app
        app.cqlsh(
            self._SEED,
            f"CREATE KEYSPACE IF NOT EXISTS {self._KS} WITH replication = "
            "{'class':'SimpleStrategy','replication_factor':2}; "
            f"CREATE TABLE IF NOT EXISTS {self._TABLE} (id int PRIMARY KEY, v text); "
            f"INSERT INTO {self._TABLE} (id,v) VALUES (1,'a'); "
            f"INSERT INTO {self._TABLE} (id,v) VALUES (2,'b'); "
            f"INSERT INTO {self._TABLE} (id,v) VALUES (3,'c'); "
            f"INSERT INTO {self._TABLE} (id,v) VALUES (4,'d'); "
            f"INSERT INTO {self._TABLE} (id,v) VALUES (5,'e');",
        )
        app.flush(self._SEED, self._KS)
        app.flush(self._PEER, self._KS)

    @mark_fault_injected
    def inject_fault(self):
        """S1 (pre-movement repair) -> bootstrap cass-join (range movement) -> S2 on cass-seed -> age out."""
        app = self.app
        seed_ip = getattr(self, "_seed_ip", None) or app.pod_ip(self._SEED)

        logger.info("[20877] S1: incremental repair on cass-seed (pre-movement)")
        app.nodetool(self._SEED, f"repair {self._KS}")

        logger.info("[20877] range movement: bootstrapping cass-join")
        app.apply_bare_pod(
            self._JOINER,
            command=self._node_cmd(),
            env={"CASSANDRA_RACK": "rack1", "CASSANDRA_SEEDS": seed_ip},
        )
        app.wait_pod_running(self._JOINER, timeout=300)
        if not app.wait_ring(3, observer_pod=self._SEED, timeout=600):
            logger.warning("[20877] inject_fault: ring did not reach 3x UN within 600s")

        for pod in (self._SEED, self._PEER, self._JOINER):
            app.flush(pod, self._KS)
        logger.info("[20877] S2: incremental repair on cass-seed (post-movement, the discriminator coordinator)")
        app.nodetool(self._SEED, f"repair {self._KS}")

        # Let the pre-movement session age past the 30s delete timeout and several cleanup passes run.
        time.sleep(95)
        probe = app.exec(self._SEED, self._COUNT_CMD)
        logger.info(f"[20877] inject_fault retained-session probe on cass-seed:\n{probe}")

    def retrigger(self):
        """Give one more cleanup pass time to run (a fixed binary would have deleted the row by now)."""
        time.sleep(45)

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._SEED,
            source="command",
            command=self._COUNT_CMD,
            pattern=self._BUGGY_PATTERN,
            retrigger=True,
            attempts=3,
            retry_delay=20.0,
        )
