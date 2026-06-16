"""CASSANDRA-16796 — pending ranges for a SHUTDOWN peer are never cleared (stale MOVING / DM).

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16796
Buggy: 4.0.0  ->  Fixed: 4.0.1 (A/B control = cassandra:4.0.1; also 3.0.25 / 3.11.11).
Component: Cluster/Membership.

THE BUG (a real 3-node single-token ring; one node shut down WHILE mid-`nodetool move`):
  A node involved in a ``nodetool move`` announces gossip ``STATUS = moving`` and its peers begin
  holding PENDING ranges for it. If that node is then GRACEFULLY shut down while it is still MOVING,
  ``Gossiper.markAsShutdown()`` publishes ``STATUS = shutdown,true`` to gossip but — in 4.0.0 — does
  NOT call ``subscriber.onChange(endpoint, ApplicationState.STATUS, shutdown)``. Because that
  notification is missing, ``TokenMetadata`` is never told the node left, so it never clears the node's
  MOVING status / pending ranges. Peers therefore keep the node as ``DM`` (Down + Moving) indefinitely,
  with phantom pending ranges that can inflate a coordinator's required-replica count and produce bogus
  ``UnavailableException`` responses to clients. The fix (4.0.1, commit
  fbb20b9162b73c4de8a82cf4ffdde3304e904603) adds the missing ``subscriber.onChange(..., STATUS,
  shutdown)`` call so ``TokenMetadata`` clears the MOVING status and pending ranges on shutdown.

VERBATIM BUGGY SIGNATURE (a surviving peer observing the moved-then-shutdown node, cassandra:4.0.0):

  (a) the node announced a GRACEFUL shutdown (not a hard crash), per peer ``nodetool gossipinfo``:
        STATUS:<gen>:shutdown,true
  (b) BUT the peer's ``nodetool status`` still shows it as MOVING -> ``DM`` (Down + Moving):
        --  Address       Load       Tokens  Owns (effective)  Host ID                               Rack
        UN  10.244.2.24   ...        1       60.8%             ...                                   rack1
        DM  10.244.3.33   68.72 KiB  1       76.2%             3cbdb42f-9e73-4f08-bfdb-96d425bc0425  rack1
        UN  10.244.3.32   ...        1       63.1%             ...                                   rack1
  (c) the peer's ``system.log`` only ever marks it DOWN, never "removed"/"state normal", so MOVING sticks:
        INFO  [GossipStage:1] ... Gossiper.java:1286 - InetAddress /10.244.3.33:7000 is now DOWN

  ``DM`` after ``shutdown,true`` IS the buggy state. A FIXED build (>= 4.0.1) fires the missing onChange
  on shutdown and the SAME move + graceful-shutdown sequence leaves the peer reading ``DN`` (Down/Normal),
  with pending ranges cleared (A/B verified on cassandra:4.0.1 — identical ``STATUS:shutdown,true`` gossip,
  but ``DN`` not ``DM``). ``DM`` is therefore a clean discriminator for 4.0.0 vs 4.0.1.

  (DOWNSTREAM, INTERMITTENT client symptom — the Jira's named symptom, NOT used for grading.) With the
  node stuck ``DM`` and both peers ``UN``, a QUORUM write to a key whose natural replicas are {a survivor
  (UP), the DM node (DOWN)} can fail with a BOGUS ``UnavailableException`` (``required_replicas: 3,
  alive_replicas: 2`` — impossible under RF=2) because the coordinator added a phantom pending replica.
  Per the Jira ("peers can *sometimes* maintain pending ranges"), that symptom is race-y; the PERSISTENT,
  deterministic part of the bug is the ``DM`` TokenMetadata state, which is what the oracle grades.

HOW THE RAW-RING HARNESS MAKES THIS RUNNABLE (the former blockers, resolved):
  * Single-token ring — ``num_tokens=1`` + an explicit ``initial_token`` per node (sed'd into
    ``cassandra.yaml`` by each pod's command). With the stock image's default vnodes (16 tokens)
    ``nodetool move`` is rejected ("This node has more than one token and cannot be moved thusly.").
    Fixed tokens make the move target deterministic: ``cass-move`` (the highest-ownership node, 76.2%)
    moves to a token between ``cass-peer`` and ``cass-seed`` so peers hold pending ranges for it.
  * Catchable MOVING window — ``nodetool setstreamthroughput 1`` on every node throttles the move's
    streaming so the node stays ``UM`` for ~30 s (stream-session setup + throttle dominate even on a
    tiny dataset), wide enough to reliably catch ``UM`` by polling before issuing the shutdown.
  * Graceful shutdown mid-move — ``replicas=0`` + bare pods: ``inject_fault`` backgrounds the move on
    ``cass-move``, polls a survivor's ``nodetool status`` until ``cass-move`` reads ``UM``, then deletes
    the ``cass-move`` pod with ``--grace-period=60`` (SIGTERM -> Cassandra drains and announces
    ``shutdown,true`` before the pod is removed; deleting the pod, rather than ``kill``-ing in place,
    avoids a kubelet restart that would re-join the node and clear ``DM``). The three bare pods are named
    ``cass-seed`` / ``cass-peer`` / ``cass-move`` — NON-ordinal names so the (replicas=0) StatefulSet
    controller does not adopt-and-delete them.
  * Detection — ``CassandraLogGrepOracle(source='command')`` runs ``nodetool status`` on the survivor
    ``cass-seed`` and matches the verbatim ``DM`` row (``^DM <ip> ...``). Present only on 4.0.0; a fixed
    4.0.1 reads ``DN`` -> no match -> bug mitigated.

Verified end-to-end on kind-fleet3: ``cass-move`` caught at ``UM``, gracefully shut down mid-move
(``STATUS:shutdown,true``), and both survivors then persistently report ``DM`` for it in ``nodetool
status`` (still ``DM`` after 120 s); the A/B-documented fixed 4.0.1 reports ``DN`` for the same sequence.
"""

import logging
import subprocess
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra16796(CassandraRawRingProblem):
    """3-node single-token ring; a node shut down mid-`nodetool move` stays DM on its peers (4.0.0)."""

    db_name = "cassandra"
    db_version = "4.0.0"
    cassandra_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    ring_namespace = "cassraw-16796"
    # Bare pods (no StatefulSet) so cass-move can be deleted (graceful shutdown) mid-move without a
    # kubelet restart, and so each node can pin an explicit single initial_token.
    replicas = 0
    num_tokens = 1

    root_cause_file = "src/java/org/apache/cassandra/gms/Gossiper.java"
    root_cause_description = (
        "On a single-token ring, a node that is mid-`nodetool move` and is then gracefully shut down "
        "leaves its peers holding phantom pending ranges for it. The graceful shutdown announces gossip "
        "STATUS shutdown,true, but in 4.0.0 Gossiper.markAsShutdown() sets the local SHUTDOWN state "
        "WITHOUT calling subscriber.onChange(endpoint, ApplicationState.STATUS, shutdown). Because that "
        "notification is missing, TokenMetadata is never told the node left and never clears its MOVING "
        "status, so peers keep the node as Down+Moving (DM) with stale pending ranges. A coordinator can "
        "then add a phantom pending replica to a write set, inflating the required-replica count beyond "
        "RF and returning bogus UnavailableException responses to clients. The fix (4.0.1, commit "
        "fbb20b9162b73c4de8a82cf4ffdde3304e904603) adds the missing subscriber.onChange(endpoint, "
        "ApplicationState.STATUS, shutdown) call in markAsShutdown() so TokenMetadata clears the MOVING "
        "status and pending ranges on shutdown. Component: Cluster/Membership."
    )

    # Bare pods — NON-ordinal names so the empty (replicas=0) StatefulSet "cass" cannot adopt/delete them.
    _SEED = "cass-seed"  # survivor + gossip seed + the observing peer the oracle reads
    _PEER = "cass-peer"  # the other survivor
    _MOVE = "cass-move"  # the highest-ownership node that is moved then gracefully shut down

    # Fixed single tokens (from the hand-repro) so the move target is deterministic and creates a
    # pending range. cass-move (76.2% owner) moves to a token between cass-peer and cass-seed.
    _SEED_TOKEN = "8051695314435402860"
    _PEER_TOKEN = "813234791936175363"
    _MOVE_TOKEN = "-3584644331145400280"
    _MOVE_TARGET = "3000000000000000000"

    _KS = "repro16796"
    _TABLE = "repro16796.t"

    # Verbatim buggy signature: the surviving peer still lists the moved-then-shutdown node as MOVING.
    # `DM <ip> ...` appears only on 4.0.0; a fixed 4.0.1 reads `DN` for the identical sequence.
    _STATUS_CMD = "nodetool status"
    _BUGGY_PATTERN = r"^DM\s+\d"

    # ── ring node command (explicit single token; PATH shim is defensive across patch images) ──
    def _node_cmd(self, token: str) -> str:
        return (
            "echo 'export PATH=$PATH:/opt/cassandra/bin:/opt/java/openjdk/bin' > /etc/profile.d/cass.sh\n"
            f"sed -ri 's/^# *initial_token:.*/initial_token: {token}/' /etc/cassandra/cassandra.yaml || true\n"
            "sed -ri 's/^(hinted_handoff_enabled:).*/\\1 false/' /etc/cassandra/cassandra.yaml || true\n"
            "exec docker-entrypoint.sh cassandra -f\n"
        )

    def post_deploy(self):
        """Stand up the 3-node single-token ring (cass-seed self-seed; peer/move seeded off its IP)."""
        app = self.app
        logger.info("[16796] post_deploy: standing up 3-node single-token ring")
        app.apply_bare_pod(
            self._SEED,
            command=self._node_cmd(self._SEED_TOKEN),
            env={"CASSANDRA_DC": "dc1", "CASSANDRA_RACK": "rack1"},
            set_seeds=False,
        )
        app.wait_pod_running(self._SEED, timeout=300)
        seed_ip = app.pod_ip(self._SEED)
        logger.info(f"[16796] seed IP={seed_ip}")
        for pod, tok in ((self._PEER, self._PEER_TOKEN), (self._MOVE, self._MOVE_TOKEN)):
            app.apply_bare_pod(
                pod,
                command=self._node_cmd(tok),
                env={"CASSANDRA_DC": "dc1", "CASSANDRA_RACK": "rack1", "CASSANDRA_SEEDS": seed_ip},
            )
        app.wait_pod_running(self._PEER, timeout=300)
        app.wait_pod_running(self._MOVE, timeout=300)
        if not app.wait_ring(3, observer_pod=self._SEED, timeout=600):
            logger.warning("[16796] post_deploy: 3-node ring did not reach 3x UN within 600s")
        self._seed_ip = seed_ip
        self._create_schema()

    def _create_schema(self):
        app = self.app
        stmts = [
            f"CREATE KEYSPACE IF NOT EXISTS {self._KS} WITH replication = "
            "{'class':'SimpleStrategy','replication_factor':2};",
            f"CREATE TABLE IF NOT EXISTS {self._TABLE} (id int PRIMARY KEY, v text);",
        ]
        stmts += [f"INSERT INTO {self._TABLE} (id,v) VALUES ({k},'init');" for k in range(1, 31)]
        app.cqlsh(self._SEED, " ".join(stmts))
        for pod in (self._SEED, self._PEER, self._MOVE):
            app.flush(pod, self._KS)

    def _graceful_shutdown(self, pod: str):
        """Delete the pod with a grace period: SIGTERM -> Cassandra drains + announces shutdown,true."""
        subprocess.run(
            f"kubectl delete pod {pod} -n {self.ring_namespace} --grace-period=60 --wait=false",
            shell=True,
            check=False,
            capture_output=True,
            text=True,
        )

    @mark_fault_injected
    def inject_fault(self):
        """Throttle streaming -> background move on cass-move -> catch UM -> graceful shutdown -> DM."""
        app = self.app
        move_ip = app.pod_ip(self._MOVE)
        logger.info(f"[16796] move node IP={move_ip} target token={self._MOVE_TARGET}")

        for pod in (self._SEED, self._PEER, self._MOVE):
            app.nodetool(pod, "setstreamthroughput 1")

        logger.info("[16796] starting background `nodetool move` on cass-move")
        app.exec(
            self._MOVE,
            f"setsid nohup nodetool move {self._MOVE_TARGET} >/var/log/cassandra/move.log 2>&1 & echo bg=$!",
        )

        # Catch the MOVING window (throttled stream keeps it ~30s) from a survivor's point of view.
        caught = False
        for attempt in range(60):
            if app.node_state(self._SEED, move_ip) == "UM":
                logger.info(f"[16796] caught cass-move at UM (poll {attempt}); gracefully shutting it down")
                self._graceful_shutdown(self._MOVE)
                caught = True
                break
            time.sleep(0.5)
        if not caught:
            logger.warning("[16796] never observed cass-move at UM — move may have completed too fast")

        # The bug: after graceful shutdown, the survivor still reports the node as MOVING -> DM.
        if app.wait_node_state(self._SEED, move_ip, "DM", timeout=120):
            logger.info("[16796] cass-seed reports cass-move as DM (Down+Moving) — buggy state established")
        else:
            logger.warning("[16796] cass-seed did not report cass-move as DM within 120s")

        probe = app.exec(self._SEED, self._STATUS_CMD)
        logger.info(f"[16796] inject_fault nodetool status on cass-seed:\n{probe}")
        gi = app.nodetool(self._SEED, "gossipinfo")
        for ln in gi.splitlines():
            if "shutdown" in ln.lower():
                logger.info(f"[16796] gossipinfo (graceful shutdown announced): {ln.strip()}")
                break

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._SEED,
            source="command",
            command=self._STATUS_CMD,
            pattern=self._BUGGY_PATTERN,
            attempts=3,
            retry_delay=10.0,
        )
