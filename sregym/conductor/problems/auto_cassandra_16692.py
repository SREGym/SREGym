"""CASSANDRA-16692: Unable to replace node with stale schema — reproduced on the raw-ring harness.

CASSANDRA-16692: Unable to replace node with stale schema.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16692
Buggy: 3.11.10   ->   Fixed: 3.11.11 (also 3.0.25, 4.0-rc1)
Component: Cluster/Schema

Reproduction summary (a MULTI-NODE RING scenario — NOT a single fresh node / single CQL):
In a Cassandra ring, shut down one node, then CREATE a new keyspace/table on a surviving
node (bumping the cluster schema version to V1) while the dead node still carries the
old/stale schema (V0) in gossip. Then replace the terminated node with a fresh non-seed
node booted with -Dcassandra.replace_address_first_boot=<deadNodeIP>. On the buggy 3.11.10
image the replacement node's startup waits for schema agreement across ALL known endpoints —
including the dead node it is replacing, whose stale schema V0 can never reconcile against the
live seed's V1 — so it blocks in JOINING ("waiting for ring information") and dies on a
schema-agreement timeout before it can join the ring. Minimal faithful repro = a 2-node ring
(1 seed + 1 victim) plus 1 replacement pod.

WHY THIS IS A STUB (do not flatten into one CQL / one fixed-image sequence):
The GenericCustomBuildProblem lifecycle deploys exactly one db_version (3.11.10) as a single
cluster and runs the `reproducer` as a CQL string against it. This bug fundamentally needs
THREE coordinated roles that a CQL string cannot express: (1) a NORMAL seed node, (2) a SECOND
"victim" node that is brought up to form the ring and then DELETED (not decommissioned) so its
stale schema persists in the seed's gossip, and (3) a THIRD "replacement" node booted as a
NON-seed with -Dcassandra.replace_address_first_boot=<victim IP>. It is ALSO not a
crash_on_startup config-gated bug: a single fresh node has no down-peer-carrying-stale-schema to
block on, so the single-image deploy->swap-buggy->wait-for-CrashLoopBackOff lifecycle CANNOT
reproduce it. The failure is a JVM startup RuntimeException on the replacement pod (pod Failed,
container exit code 3), not a CQL result. There is no `reproducer` CQL you can run against a
deployed cluster that fires this fault, so the full multi-node steps are transcribed below and
`continuous_reproducer` is left False (no working single-cluster looping reproducer pod). See the
authoritative evidence log: .claude/repro-evidence/repro-CASSANDRA-16692.md

Verbatim buggy signature (from the reproduction evidence log; replacement pod, cassandra:3.11.10):
  ERROR [main] CassandraDaemon.java:803 - Exception encountered during startup
  java.lang.RuntimeException: Didn't receive schemas for all known versions within the timeout
        at org.apache.cassandra.service.StorageService.waitForSchema(StorageService.java:947)
        at org.apache.cassandra.service.StorageService.joinTokenRing(StorageService.java:987)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:753)
        at org.apache.cassandra.service.StorageService.initServer(StorageService.java:687)
        at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:395)
        at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:633)
        at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:786)
(Replacement pod exited phase=Failed, container exitCode=3.) A/B control on fixed 3.11.11: the
identical sequence has ZERO occurrences of this message — the replaced/down node is exempted from
the schema-agreement wait, so the replacement bootstraps and joins the ring (UN).
"""

import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Schema bumped on the surviving seed while the victim is down, so the victim's gossip entry
# keeps the OLD schema version (V0) and the seed advances to V1 — the divergence the bug needs.
_KS = "ks16692"
_TABLE = f"{_KS}.t"
# Verbatim startup-abort the buggy 3.11.10 replacement dies with (see module docstring).
_ABORT_PATTERN = r"Didn't receive schemas for all known versions within the timeout"


class AutoCassandra16692(CassandraRawRingProblem):
    """seed cass-0 (survivor) + in-pod `victim` (shutdown, stale schema) + `replacement`.

    Realised through the benchmark architecture: ``deploy_app`` stands up a stock
    ``cassandra:3.11.10`` seed (cass-0) plus two bare pods parked at ``tail -f /dev/null``
    (``victim`` and ``replacement``). ``inject_fault`` launches an in-pod CassandraDaemon on
    ``victim`` so it forms a 2-node ring, shuts it down (graceful, leaving a stale-schema gossip
    entry), bumps the seed schema via ``CREATE KEYSPACE/TABLE``, then launches an in-pod daemon on
    ``replacement`` with ``-Dcassandra.replace_address_first_boot=<victim IP>``. On 3.11.10 the
    replacement blocks in ``waitForSchema`` on the dead victim's unreconcilable V0 and aborts with
    ``RuntimeException: Didn't receive schemas for all known versions within the timeout``; the
    ``CassandraLogGrepOracle`` greps the replacement's startup log for that line. (Fixed 3.11.11
    exempts the replaced node and joins cleanly.)
    """

    db_name = "cassandra"
    db_version = "3.11.10"
    cassandra_version = "3.11.10"
    source_git_ref = "cassandra-3.11.10"
    ring_namespace = "cassraw-16692"
    # cass-0 = the surviving seed; victim/replacement run as in-pod daemons in bare pods so a
    # StatefulSet controller never resurrects the deleted victim.
    replicas = 1
    extra_pods = [
        {"pod_name": "victim", "command": "tail -f /dev/null"},
        {"pod_name": "replacement", "command": "tail -f /dev/null"},
    ]

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "Unable to replace a terminated node when a stale schema lingers in gossip. After "
        "CASSANDRA-15158, StorageService.joinTokenRing calls waitForSchema "
        "(StorageService.java:947), which blocks startup until it receives schema for ALL known "
        "endpoints — including the down node being replaced. The dead node's stale schema "
        "version (V0) can never reconcile against the live seed's newer version (V1, bumped by a "
        "CREATE KEYSPACE/TABLE issued while the victim was down), so on the buggy 3.11.10 image "
        "the replacement node sits in JOINING ('waiting for ring information') and dies with "
        "RuntimeException: 'Didn't receive schemas for all known versions within the timeout' "
        "before it can join the ring. Buggy 3.11.10 has CASSANDRA-15158 but not the "
        "CASSANDRA-16692 fix, which exempts the replaced node from the schema-agreement wait."
    )

    _SEED = "cass-0"
    _VICTIM = "victim"
    _REPLACEMENT = "replacement"
    _REPL_LOG = "/var/log/cassandra/replacement.log"

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
        victim_ip = app.pod_ip(self._VICTIM)
        logger.info(f"[16692] seed={self._SEED} victim={self._VICTIM}@{victim_ip} replacement={self._REPLACEMENT}")

        # STEP 1 — launch the victim daemon so it forms a 2-node ring (seed sees it UN).
        logger.info("[16692] STEP1 launch victim daemon, wait for 2x UN")
        app.launch_daemon(self._VICTIM, log_file="/var/log/cassandra/victim.log")
        if not app.wait_node_state(self._SEED, victim_ip, "UN", timeout=300):
            logger.warning("[16692] STEP1: seed did not see victim UN within 300s")

        # STEP 2 — gracefully shut the victim down (SIGTERM => Cassandra announces shutdown), so its
        # gossip entry persists on the seed with the OLD schema while the process is gone.
        logger.info("[16692] STEP2 graceful-shutdown victim (SIGTERM), wait DN")
        app.exec(self._VICTIM, "pkill -TERM -f CassandraDaemon || true")
        if not app.wait_node_state(self._SEED, victim_ip, "DN", timeout=180):
            logger.warning("[16692] STEP2: seed did not mark victim DN within 180s")
        gi = app.exec(
            self._SEED,
            f"nodetool gossipinfo 2>/dev/null | grep -A12 {victim_ip} | grep -iE 'STATUS|SCHEMA|shutdown' | head -4 || true",
        )
        logger.info(f"[16692] STEP2 seed gossip for victim:\n{gi.strip()}")

        # STEP 3 — bump the seed schema (CREATE KEYSPACE/TABLE) so it diverges from the victim's V0.
        logger.info("[16692] STEP3 CREATE KEYSPACE/TABLE on seed (schema V0 -> V1)")
        app.cqlsh(
            self._SEED,
            f"CREATE KEYSPACE IF NOT EXISTS {_KS} WITH replication = "
            "{'class':'SimpleStrategy','replication_factor':1}; "
            f"CREATE TABLE IF NOT EXISTS {_TABLE} (id int PRIMARY KEY, v text);",
        )
        sv = app.exec(self._SEED, "nodetool describecluster 2>/dev/null | sed -n '1,12p' || true")
        logger.info(f"[16692] STEP3 describecluster:\n{sv.strip()}")

        # STEP 4 — launch the replacement (non-seed) with replace_address_first_boot=<victim IP>.
        # On 3.11.10 it blocks in waitForSchema on the victim's unreconcilable V0 and aborts.
        logger.info("[16692] STEP4 launch replacement with replace_address_first_boot")
        app.wipe_data(self._REPLACEMENT)
        app.launch_daemon(
            self._REPLACEMENT,
            jvm_extra_opts=f"-Dcassandra.replace_address_first_boot={victim_ip}",
            log_file=self._REPL_LOG,
        )

        # STEP 5 — wait for the verbatim startup abort on the replacement's log.
        logger.info("[16692] STEP5 wait for replacement schema-agreement startup abort")
        captured = self._wait_log(
            self._REPLACEMENT,
            self._REPL_LOG,
            ("Didn't receive schemas for all known versions within the timeout",),
            timeout=240,
        )
        if captured:
            logger.info(f"[16692] inject_fault captured buggy startup-abort signature:\n{captured}")
        else:
            logger.warning("[16692] startup-abort signature not observed within 240s")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._REPLACEMENT,
            source="command",
            command=f"cat {self._REPL_LOG} 2>/dev/null || true",
            pattern=_ABORT_PATTERN,
            attempts=4,
            retry_delay=10.0,
        )
