"""STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

CASSANDRA-15433: Pending ranges are not recalculated on keyspace creation.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-15433
Buggy: 4.0.1   ->   Fixed: 4.0.2 (also 3.0.26, 3.11.12, 4.1-alpha1, 4.1)
Component: Cluster/Membership

Reproduction summary (a MULTI-NODE RING scenario — NOT a single fresh node / single CQL):
A 2-node ring (both NORMAL/UN) plus a third node that is held in BOOT/bootstrap mode
(observed as UJ = Up/Joining in `nodetool status`, pinned in the pending-range window via
`-Dcassandra.ring_delay_ms=600000`). While that node is in BOOT, a keyspace `ks15433`
(SimpleStrategy RF=2) + table + 50 INSERTs are issued through a NORMAL coordinator (cass-0).
Because the keyspace did NOT exist when the joining node's BOOT state change was observed,
pending ranges are not recalculated on its creation, so the joining node is excluded from
all writes for that keyspace. The coordinator's own `SELECT count(*)` returns the correct
50 (the bug is invisible from CQL); the loss is only visible as a `nodetool cfstats` metric
read ON the joining node, where it has received zero of the RF=2 writes.

WHY THIS IS A STUB (do not flatten into one CQL / one fixed-image sequence):
The GenericCustomBuildProblem lifecycle deploys exactly one db_version (4.0.1) as a single
cluster and runs the `reproducer` as a CQL string against it. This bug fundamentally needs
THREE coordinated roles that a CQL string cannot express: (1) two NORMAL ring members, (2) a
THIRD node parked in BOOT/UJ for the duration of the writes, and (3) the writes issued through
one of the NORMAL nodes while the third is still UJ. The signature is not a CQL result at all —
it is a per-node `nodetool cfstats` metric (`Local write count: 0`) read on the specific joining
node. There is no `reproducer` CQL you can run against a deployed cluster that fires or observes
this fault, so the full multi-node steps are transcribed below and `continuous_reproducer` is
left False (no working single-cluster looping reproducer pod). See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-15433.md

Verbatim buggy signature (from the reproduction evidence log):
  Local write count: 0
(on the joiner's ks15433.t, after 50 RF=2 writes through a NORMAL coordinator while the joiner
is in BOOT/UJ mode; equivalently `Write Count: 0` at the keyspace level). A/B control: the same
workload on fixed 4.0.2 routes writes to the pending replica, giving `Local write count: 36`.
"""

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

_KS = "ks15433"
_TABLE = f"{_KS}.t"
# Keep the joiner parked in BOOT (UJ) for the whole run so the oracle can read its
# pending-range-window state at mitigation time; the run finishes well within this window.
_RING_DELAY_MS = 1800000


class AutoCassandra15433(CassandraRawRingProblem):
    """Pending-ranges-not-recalculated-on-keyspace-creation bug, on a raw 3-node ring.

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:4.0.1`` 2-node ring; ``inject_fault`` adds a third ``joiner`` pod held in
    BOOT/UJ via ``-Dcassandra.ring_delay_ms`` and, while it is UJ, creates ``ks15433``
    (RF=2) + 50 writes through the NORMAL coordinator cass-0. The
    ``CassandraLogGrepOracle`` reads ``nodetool cfstats ks15433.t`` on the joiner and grades
    the bug present when its ``Local write count`` is 0 (buggy 4.0.1 excluded the joiner from
    the writes) vs non-zero (fixed 4.0.2 routes writes to the pending replica).
    """

    db_name = "cassandra"
    db_version = "4.0.1"
    cassandra_version = "4.0.1"
    source_git_ref = "cassandra-4.0.1"
    ring_namespace = "cassraw-15433"
    replicas = 2

    root_cause_file = "src/java/org/apache/cassandra/schema/Schema.java"
    root_cause_description = (
        "Pending ranges are not recalculated on keyspace creation. When a node begins "
        "bootstrapping, Cassandra recalculates pending token ranges for each keyspace that "
        "EXISTS at the moment the BOOT/BOOT_REPLACE state change is observed "
        "(StorageService.handleState* -> PendingRangeCalculatorService). When a keyspace is "
        "CREATED *after* that, while a node is still in BOOT, the schema-merge path "
        "(Schema.merge, on CREATE KEYSPACE) does NOT trigger a pending-range recalculation for "
        "the joining node. As a result writes for the newly created keyspace are not routed to "
        "the joining node as a pending replica, so once bootstrap completes the joined node is "
        "silently missing all data written to that keyspace during the BOOT window. The fix "
        "(4.0.2) recalculates pending ranges when a keyspace is created so the joining node "
        "receives writes for its pending ranges."
    )

    @mark_fault_injected
    def inject_fault(self):
        """Park a third node in BOOT/UJ, then create ks15433 + 50 writes while it is joining."""
        import logging

        log = logging.getLogger(__name__)
        app = self.app

        # PHASE 1 — confirm the 2-node ring is NORMAL, then add the joiner in BOOT.
        app.wait_ring(2)
        joiner_ip = app.launch_joiner("joiner", ring_delay_ms=_RING_DELAY_MS)
        if not app.wait_node_state("cass-0", joiner_ip, "UJ", timeout=240):
            log.warning("[15433] joiner did not reach UJ within timeout")
        log.info(f"[15433] ring status with joiner:\n{app.nodetool('cass-0', 'status')}")

        # PHASE 2 — create the keyspace + table AFTER the joiner's BOOT was observed.
        app.cqlsh(
            "cass-0",
            f"CREATE KEYSPACE IF NOT EXISTS {_KS} WITH replication = "
            "{'class':'SimpleStrategy','replication_factor':2}; "
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (id int PRIMARY KEY, v text);",
        )

        # PHASE 3 — 50 RF=2 writes through the NORMAL coordinator while the joiner is UJ.
        inserts = " ".join(f"INSERT INTO {_TABLE} (id,v) VALUES ({i},'v{i}');" for i in range(1, 51))
        app.cqlsh("cass-0", inserts)
        log.info(f"[15433] coordinator count(*):\n{app.cqlsh('cass-0', f'SELECT count(*) AS n FROM {_TABLE};')}")

        # PHASE 4 (proof at inject time) — cfstats on the joiner.
        log.info(f"[15433] inject_fault joiner cfstats:\n{app.exec('joiner', f'nodetool cfstats {_TABLE} 2>&1')}")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod="joiner",
            source="command",
            command=f"nodetool cfstats {_TABLE} 2>&1",
            pattern=r"Local write count:\s*0\s*$",
        )
