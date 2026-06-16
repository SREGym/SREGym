"""CASSANDRA-16577: Node waits for schema agreement on removed nodes.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16577
Buggy: 3.11.10  ->  Fixed: 3.11.11 (also 3.0.25, 4.0-rc1, 4.0).

Reproduced on a raw multi-node ring through ``CassandraRawRingProblem`` — see steps below.

Reproduction summary (real multi-node ring; NOT expressible as one CQL string):
A 2-node ring is formed (cass1 seed + cassb). cassb is decommissioned (`nodetool decommission`)
and its pod is deleted, so it lingers in cass1's gossip with STATUS:LEFT and a now-stale SCHEMA
version. `CREATE KEYSPACE k` is then run on cass1, advancing cass1's schema while the removed
node's gossip entry keeps the OLD version. A fresh-identity node (cassc) is launched to bootstrap;
its join-time schema-agreement wait counts the removed node's stale gossip schema, never reaches
agreement, and aborts startup. (Discriminating test in the evidence log proved this fires at the
general join-time wait even with allocate_tokens_for_keyspace UNSET — the Jira reporter hit the same
defective waitForSchema via the allocate_tokens_for_keyspace -> allocateTokens path.)

Verbatim buggy signature (cassc, cassandra:3.11.10):
    WARN  [main] StorageService.java:941 - There are nodes in the cluster with a different schema
    version than us we did not merged schemas from, our version : (c527aae7-...), outstanding
    versions -> endpoints : {e84b6a60-24cf-30ca-9b58-452d92911703=[/10.244.3.105]}
    Exception (java.lang.RuntimeException) encountered during startup: Didn't receive schemas for
    all known versions within the timeout
    java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
        at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947)
        at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687)
        at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395)
        at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633)
        at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786)

NOTE: The fault is a startup abort driven by cluster gossip state (a decommissioned node
lingering with a divergent schema version). It CANNOT be reproduced by a single ``reproducer``
CQL string run against one cluster; it requires multi-pod orchestration (form ring ->
decommission + remove -> CREATE KEYSPACE -> bootstrap a fresh node). That orchestration is
realised here on a raw ring through ``CassandraRawRingProblem``: ``deploy_app`` stands up a stock
``cassandra:3.11.10`` seed plus two bare pods; ``inject_fault`` walks the steps with in-pod
CassandraDaemons; and the ``CassandraLogGrepOracle`` greps the fresh bootstrapper's startup log
for the verbatim abort. See the authoritative evidence log:
.claude/repro-evidence/repro-CASSANDRA-16577.md
"""

import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Keyspace created on the seed AFTER the peer leaves, so the removed (LEFT) node's gossip entry
# keeps the OLD schema version while the seed advances — the divergence that blocks the join.
_KS = "ks16577"
# Verbatim startup-abort the buggy 3.11.10 bootstrapper dies with (see module docstring).
_ABORT_PATTERN = r"Didn't receive schemas for all known versions within the timeout"


class AutoCassandra16577(CassandraRawRingProblem):
    """seed cass-0 (survivor) + in-pod `decom` (decommissioned, stale schema) + fresh `joiner`.

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:3.11.10`` seed (cass-0) plus two bare pods parked at ``tail -f /dev/null``
    (``decom`` and ``joiner``). ``inject_fault`` launches an in-pod CassandraDaemon on ``decom``
    so it forms a 2-node ring, decommissions it (leaving a STATUS:LEFT gossip entry with a stale
    schema), bumps the seed schema via ``CREATE KEYSPACE``, then launches a fresh in-pod daemon on
    ``joiner``. On 3.11.10 the joiner's join-time ``waitForSchema`` counts the removed node's stale
    gossip schema, never reaches agreement, and aborts with ``RuntimeException: Didn't receive
    schemas for all known versions within the timeout``; the ``CassandraLogGrepOracle`` greps the
    joiner's startup log for that line. (Fixed 3.11.11 ignores removed nodes and the joiner joins.)
    """

    db_name = "cassandra"
    db_version = "3.11.10"
    cassandra_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    ring_namespace = "cassraw-16577"
    # cass-0 = the surviving seed; decom/joiner run as in-pod daemons in bare pods so a
    # StatefulSet controller never resurrects the decommissioned node.
    replicas = 1
    extra_pods = [
        {"pod_name": "decom", "command": "tail -f /dev/null"},
        {"pod_name": "joiner", "command": "tail -f /dev/null"},
    ]

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "A node bootstrapping into a ring aborts startup with RuntimeException 'Didn't receive "
        "schemas for all known versions within the timeout'. StorageService.waitForSchema (the "
        "join-time schema-agreement wait reached from joinTokenRing) counts the gossip-advertised "
        "schema version of a node that has already been decommissioned/removed (STATUS:LEFT). When "
        "a keyspace is created after that node leaves, the cluster's schema advances while the "
        "removed node's lingering gossip entry keeps the OLD version, so its stale version stays in "
        "the set of outstanding schema versions, agreement is never reached, and the new node never "
        "joins. The fix (3.11.11) stops waiting on schema versions from removed nodes."
    )

    _SEED = "cass-0"
    _DECOM = "decom"
    _JOINER = "joiner"
    _JOINER_LOG = "/var/log/cassandra/joiner.log"

    def _wait_log(self, pod: str, log_file: str, markers: tuple[str, ...], timeout: int) -> str:
        """Poll a pod's launch log until any marker substring appears; return the matched line."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.app.exec(pod, f"cat {log_file} 2>/dev/null || true")
            for ln in text.splitlines():
                if any(m in ln for m in markers):
                    return ln.strip()
            time.sleep(4)
        return ""

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        decom_ip = app.pod_ip(self._DECOM)
        logger.info(f"[16577] seed={self._SEED} decom={self._DECOM}@{decom_ip} joiner={self._JOINER}")

        # STEP 1 — launch the decom daemon so it forms a 2-node ring (seed sees it UN).
        logger.info("[16577] STEP1 launch decom daemon, wait for 2x UN")
        app.launch_daemon(self._DECOM, log_file="/var/log/cassandra/decom.log")
        if not app.wait_node_state(self._SEED, decom_ip, "UN", timeout=300):
            logger.warning("[16577] STEP1: seed did not see decom UN within 300s")

        # STEP 2 — decommission decom so it leaves the ring (STATUS:LEFT), then kill the daemon.
        # The LEFT gossip entry (with the now-stale schema) lingers on the seed.
        logger.info("[16577] STEP2 nodetool decommission decom, wait for it to leave the ring")
        out = app.exec(self._DECOM, "nodetool decommission 2>&1; echo rc=$?", timeout=300)
        logger.info(f"[16577] STEP2 decommission output tail:\n{out.strip()[-400:]}")
        # Wait until the seed no longer lists decom as a live (UN) ring member.
        deadline = time.time() + 120
        while time.time() < deadline and app.node_state(self._SEED, decom_ip) == "UN":
            time.sleep(4)
        app.kill_daemon(self._DECOM)
        gi = app.exec(
            self._SEED,
            f"nodetool gossipinfo 2>/dev/null | grep -A12 {decom_ip} | grep -iE 'STATUS|SCHEMA|LEFT' | head -4 || true",
        )
        logger.info(f"[16577] STEP2 seed gossip for decom (expect STATUS:...LEFT + stale SCHEMA):\n{gi.strip()}")

        # STEP 3 — bump the seed schema (CREATE KEYSPACE) so it diverges from decom's stale V0.
        logger.info("[16577] STEP3 CREATE KEYSPACE on seed (schema advances; removed node stays on old version)")
        app.cqlsh(
            self._SEED,
            f"CREATE KEYSPACE IF NOT EXISTS {_KS} WITH replication = "
            "{'class':'SimpleStrategy','replication_factor':1};",
        )
        sv = app.exec(self._SEED, "nodetool describecluster 2>/dev/null | sed -n '1,12p' || true")
        logger.info(f"[16577] STEP3 describecluster:\n{sv.strip()}")

        # STEP 4 — launch a fresh bootstrapper. On 3.11.10 its join-time waitForSchema counts the
        # removed node's stale schema and aborts.
        logger.info("[16577] STEP4 launch fresh joiner daemon")
        app.wipe_data(self._JOINER)
        app.launch_daemon(self._JOINER, log_file=self._JOINER_LOG)

        # STEP 5 — wait for the verbatim startup abort on the joiner's log.
        logger.info("[16577] STEP5 wait for joiner schema-agreement startup abort")
        captured = self._wait_log(
            self._JOINER,
            self._JOINER_LOG,
            ("Didn't receive schemas for all known versions within the timeout",),
            timeout=240,
        )
        if captured:
            logger.info(f"[16577] inject_fault captured buggy startup-abort signature:\n{captured}")
        else:
            logger.warning("[16577] startup-abort signature not observed within 240s")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._JOINER,
            source="command",
            command=f"cat {self._JOINER_LOG} 2>/dev/null || true",
            pattern=_ABORT_PATTERN,
            attempts=4,
            retry_delay=10.0,
        )
