"""CASSANDRA-21132: SAI INDEX_STATUS legacy-encoding overflow deadlocks gossip after a cold restart.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-21132
Buggy: cassandra:5.0.6  ->  Fixed (opt-in flag added): cassandra:5.0.7

Reproduced on a raw 2-node 5.0.6 ring carrying ~328 SAI indexes (4 keyspaces x 2 tables x 41 SAI
indexes, every identifier padded to the 48-char max to bloat the legacy gossip payload). The schema
is loaded while both nodes are NORMAL (the compressed numeric INDEX_STATUS format is used), then BOTH
in-pod CassandraDaemons are SIGTERM'd (PID 1 is the java daemon, so its JVM shutdown hook runs and the
kubelet restarts the container in place; the ``emptyDir`` data volume survives a container restart, so
the ~328-index schema persists) — a full cold bring-down/bring-up.

During the post-restart gossip convergence, Gossiper.getMinVersion() returns unknown (no peer
RELEASE_VERSION advertised yet), so IndexStatusManager falls back to the pre-5.0.3 LEGACY INDEX_STATUS
encoding (full keyspace name duplicated per index entry + literal status strings like
"BUILD_SUCCEEDED" instead of numeric codes). With ~328 indexes the encoded value exceeds
Short.MAX_VALUE (32767 bytes), so serializing the GossipDigestAck trips the bare
``assert length <= Short.MAX_VALUE`` in TypeSizes.sizeof. The ACK is never sent, the node stays DOWN,
gossip never converges, the compressed format is never re-enabled, and the node loops the error every
~5s (JVMStabilityInspector logs it but does not halt the JVM) = startup deadlock.

IMPORTANT — the fix is OPT-IN, not automatic: CASSANDRA-21132 does NOT repair the getMinVersion()
convergence race; it adds a cassandra.yaml option ``force_optimized_index_status_format`` (default
false). So stock 5.0.7 with the default config STILL reproduces; the documented positive control is to
set ``force_optimized_index_status_format: true``.

VERBATIM BUGGY SIGNATURE (cass-0 system.log, Thread[GossipStage:1]):
  java.lang.RuntimeException: java.lang.AssertionError
    ...
  Caused by: java.lang.AssertionError: null
    at org.apache.cassandra.db.TypeSizes.sizeof(TypeSizes.java:44)
    at org.apache.cassandra.gms.VersionedValue$VersionedValueSerializer.serializedSize(VersionedValue.java:381)
    at org.apache.cassandra.gms.EndpointStateSerializer.serializedSize(EndpointState.java:401)
    at org.apache.cassandra.gms.GossipDigestAckSerializer.serializedSize(GossipDigestAck.java:96)
    at org.apache.cassandra.gms.GossipDigestSynVerbHandler.doVerb(GossipDigestSynVerbHandler.java:110)

The oracle greps cass-0's system.log for the verbatim ``TypeSizes.sizeof(TypeSizes.java:44)`` frame;
on fixed binaries with the optimized format forced the cluster converges and the line never appears.
"""

import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Bloated SAI schema dimensions: 4 keyspaces x 2 tables x 41 SAI indexes = 328 indexes. With every
# identifier padded to 48 chars the legacy INDEX_STATUS encoding exceeds Short.MAX_VALUE (32767 bytes).
_K, _T, _I = 4, 2, 41
_PATH_FIX = "echo 'export PATH=/opt/cassandra/bin:/opt/cassandra/tools/bin:$PATH' > /etc/profile.d/cass.sh"
_SIGNATURE = r"TypeSizes\.sizeof\(TypeSizes\.java:44\)"


def _name(prefix: str, uid: int) -> str:
    """A unique <=48-char identifier: prefix+uid underscore-padded to the 48-char max."""
    return (f"{prefix}{uid}" + "_" * 48)[:48]


class AutoCassandra21132(CassandraRawRingProblem):
    """SAI INDEX_STATUS legacy-encoding gossip overflow on a raw 2-node 5.0.6 ring after a cold restart.

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock ``cassandra:5.0.6``
    ring; ``inject_fault`` loads ~328 bloated SAI indexes and cold-restarts both in-pod daemons; the
    ``CassandraLogGrepOracle`` greps cass-0's system.log for the verbatim
    ``TypeSizes.sizeof(TypeSizes.java:44)`` assertion that the overflowing legacy gossip encoding trips.
    """

    db_name = "cassandra"
    db_version = "5.0.6"
    cassandra_version = "5.0.6"
    source_git_ref = "cassandra-5.0.6"
    ring_namespace = "cassraw-21132"
    replicas = 2
    hinted_handoff_enabled = False

    root_cause_file = "src/java/org/apache/cassandra/index/IndexStatusManager.java"
    root_cause_description = (
        "Startup deadlock on a homogeneous 5.0.x cluster with many SAI indexes after a full cold "
        "bring-down/bring-up. During gossip convergence, Gossiper.getMinVersion() returns unknown "
        "(no peer RELEASE_VERSION advertised yet), so IndexStatusManager falls back to the pre-5.0.3 "
        "legacy INDEX_STATUS encoding — it duplicates the full keyspace name per index entry and "
        'writes status strings ("BUILD_SUCCEEDED") instead of numeric codes. With enough indexes '
        "the encoded value exceeds Short.MAX_VALUE (32767 bytes), so serializing the GossipDigestAck "
        "trips a bare `assert length <= Short.MAX_VALUE` in TypeSizes.sizeof (TypeSizes.java:44, hence "
        "message null). The ACK is never sent, the joining node stays DOWN, gossip never converges, "
        "the compressed format is never re-enabled, and the cluster deadlocks. The fix is OPT-IN: it "
        "adds a force_optimized_index_status_format cassandra.yaml option (default false) rather than "
        "fixing the convergence race, so stock 5.0.7 with the default config still reproduces. "
        "(Exact source path inferred from the JIRA title; the convergence helper is Gossiper.getMinVersion.)"
    )

    def _pods(self):
        return [f"cass-{i}" for i in range(self.replicas)]

    def post_deploy(self):
        """Restore the cassandra tool PATH (5.0.x quirk) so nodetool/cqlsh work before schema load."""
        for pod in self._pods():
            self.app.exec(pod, _PATH_FIX)

    def _build_schema(self):
        """Load K x T x I SAI indexes with 48-char identifiers (idempotent via IF NOT EXISTS)."""
        gcol = 0
        for k in range(_K):
            ks = _name("ksx", k)
            self.app.cqlsh(
                "cass-0",
                f"CREATE KEYSPACE IF NOT EXISTS {ks} WITH replication = "
                "{'class':'SimpleStrategy','replication_factor':2};",
                timeout=120,
            )
            for t in range(_T):
                tbl = _name("tb", k * 100 + t)
                pairs = [(_name("col", gcol + j), _name("idx", gcol + j)) for j in range(_I)]
                gcol += _I
                cols = ", ".join(f"{col} int" for col, _ in pairs)
                self.app.cqlsh(
                    "cass-0",
                    f"CREATE TABLE IF NOT EXISTS {ks}.{tbl} (pk int PRIMARY KEY, {cols});",
                    timeout=120,
                )
                stmts = [f"CREATE INDEX IF NOT EXISTS {ix} ON {ks}.{tbl} ({col}) USING 'sai';" for col, ix in pairs]
                for s in range(0, len(stmts), 12):
                    self.app.cqlsh("cass-0", " ".join(stmts[s : s + 12]), timeout=300)
        cnt = self.app.cqlsh("cass-0", "SELECT count(*) FROM system_schema.indexes;", timeout=60)
        logger.info(f"[21132] built bloated SAI schema (~{_K * _T * _I} indexes); count query:\n{cnt}")

    def _restarted(self, pod: str) -> bool:
        """True once the container has restarted in place.

        On a raw StatefulSet ring pod PID 1 *is* the java CassandraDaemon, so nodetool/pkill
        cannot recycle it and ``pgrep -f CassandraDaemon`` self-matches the probe's own exec shell
        (its argv contains the string) = useless as a liveness signal. Instead we use the PATH-fix
        file (/etc/profile.d/cass.sh, written on the *ephemeral* container root fs) as a sentinel:
        a container restart resets that fs, so the file disappears exactly when — and only when —
        the container has actually restarted in place.
        """
        out = self.app.exec(pod, "test -f /etc/profile.d/cass.sh && echo PRESENT || echo ABSENT")
        return "ABSENT" in out

    def _cold_restart(self):
        """Cold-restart both nodes by SIGTERM'ing the in-pod PID 1 (the CassandraDaemon).

        Sending SIGTERM to PID 1 fires the JVM shutdown hook; PID 1 exits and the kubelet restarts
        the container in place. The ``emptyDir`` data volume survives a container restart so the
        ~328-index schema persists, but the container root fs is reset (so the PATH fix must be
        re-applied — and its disappearance is the reliable "restart happened" sentinel above). The
        post-restart gossip convergence race then selects the legacy INDEX_STATUS encoding which
        overflows Short.MAX_VALUE and trips the TypeSizes.sizeof assertion. (``kill_daemon``/pkill is
        a no-op here: SIGKILL to PID 1 from inside the container's own PID namespace is ignored and
        the ``-f CassandraDaemon`` match hits the exec shell itself.)
        """
        for pod in self._pods():
            logger.info(f"[21132] SIGTERM pid 1 on {pod}: {self.app.exec(pod, 'kill -s TERM 1').strip()}")
        time.sleep(20)  # let the JVM shutdown hooks run and the kubelet begin restarting the containers
        deadline = time.time() + 300
        while time.time() < deadline:
            if all(self._restarted(pod) for pod in self._pods()):
                break
            time.sleep(5)
        time.sleep(20)  # let the fresh JVMs reach gossip before we probe / re-apply the PATH fix
        for pod in self._pods():
            self.app.exec(pod, _PATH_FIX)
        logger.info("[21132] both containers cold-restarted (emptyDir schema preserved); PATH fix re-applied")

    @mark_fault_injected
    def inject_fault(self):
        """Load the bloated SAI schema, then cold-restart both nodes to trip the gossip overflow."""
        self._build_schema()
        self._cold_restart()
        # Give the post-restart gossip exchange time to hit the overflow and start looping the error.
        # Both nodes hold the bloated INDEX_STATUS and re-advertise it, so the assertion surfaces on
        # each node's system.log; return as soon as either shows it (the oracle re-checks cass-0).
        for _ in range(24):
            time.sleep(10)
            for pod in self._pods():
                hits = self.app.grep_log(pod, _SIGNATURE, source="system_log")
                if hits:
                    logger.info(f"[21132] inject_fault saw overflow signature on {pod}: {hits[0].strip()}")
                    return
        logger.warning("[21132] inject_fault did not yet observe the overflow signature (oracle will retry)")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod="cass-0",
            source="system_log",
            pattern=_SIGNATURE,
            retrigger=False,
            attempts=10,
            retry_delay=15.0,
        )
