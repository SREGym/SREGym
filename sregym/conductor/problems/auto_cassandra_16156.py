"""CASSANDRA-16156: Decommissioned nodes are picked for gossip when unreachable nodes are considered.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16156

Buggy: 3.11.8  ->  Fixed: 3.11.9 (also 3.0.23, 4.0).
Components: Cluster/Gossip.

Reproduction summary (2-node ring; a 2-node ring is sufficient and fastest):
  Bring up a 2-node ring on cassandra:3.11.8 (cass-0 = seed/survivor, cass-1 = victim).
  `nodetool decommission` cass-1 so it transitions to LEFT and leaves the ring, then scale the
  StatefulSet to 1 replica so cass-1's pod and its port 7000 disappear (becomes unreachable).
  With DEBUG enabled at runtime on the survivor for org.apache.cassandra.net.OutboundTcpConnection
  and org.apache.cassandra.gms.Gossiper (nodetool setlogginglevel, no restart), the survivor KEEPS
  selecting the departed LEFT node for gossip via the unreachable-member path
  (Gossiper.maybeGossipToUnreachableMember -> sendGossip) and repeatedly tries to connect to it,
  logging connection failures on the MessagingService-Outgoing-/<ip>-Gossip thread. The released
  fix (cassandra:3.11.9) stops selecting the LEFT node: under the identical workload there are
  ZERO further connect attempts and ZERO connection failures after the victim is convicted LEFT/DOWN.

This bug is purely about cross-node gossip topology — it needs a real 2-node ring so one node can be
DECOMMISSIONED (-> LEFT) and then made UNREACHABLE (scale the StatefulSet down so the departed pod's
port 7000 disappears), and the SURVIVING peer is the thing under test. It is realised here on a raw
2-node ring: `deploy_app` stands up a stock `cassandra:3.11.8` ring; `inject_fault` decommissions the
victim, enables DEBUG on the survivor and scales the StatefulSet down; the `CassandraLogGrepOracle`
greps the survivor's /var/log/cassandra/debug.log for the gossip-to-a-departed-node connect failure.

Verbatim buggy signature (cass-0 /var/log/cassandra/debug.log, cassandra:3.11.8 — DEBUG, OLD
OutboundTcpConnection stack, port 7000, real pod IPs):
  DEBUG [MessagingService-Outgoing-/10.244.2.125-Gossip] ... OutboundTcpConnection.java:546 - Unable to connect to /10.244.2.125
  java.net.ConnectException: Connection timed out
      at org.apache.cassandra.net.OutboundTcpConnectionPool.newSocket(OutboundTcpConnectionPool.java:146) ...
      at org.apache.cassandra.net.OutboundTcpConnection.connect(OutboundTcpConnection.java:434) ...
      at org.apache.cassandra.net.OutboundTcpConnection.run(OutboundTcpConnection.java:262) ...
(Sustained: repeated "Unable to connect to /<ip>" failures on the -Gossip thread, all AFTER the
victim was convicted LEFT/DOWN. The LEFT node keeps being selected as long as it stays in the gossip
endpoint-state map.) The fixed image 3.11.9 produces ZERO such lines for the departed node.
"""

import logging
import re
import subprocess
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

_SURVIVOR = "cass-0"
_VICTIM = "cass-1"
_DEBUG_LOG = "/var/log/cassandra/debug.log"

# Thread-scoped signature: only a gossip connection to the departed (LEFT, now unreachable) peer
# logs "Unable to connect to /<ip>" on a MessagingService-Outgoing-/<ip>-Gossip thread. After the
# decommissioned victim's pod is deleted it is the ONLY unreachable endpoint, so any such line is
# the bug. The fixed 3.11.9 stops selecting the LEFT node and produces zero such lines.
_SIGNATURE = r"MessagingService-Outgoing-/\S+-Gossip\].*Unable to connect to /"
# Pre-filter the (potentially large) debug.log to the candidate lines; the oracle then applies the
# thread-scoped regex above.
_GREP_CMD = f"grep -F 'Unable to connect to /' {_DEBUG_LOG} 2>/dev/null | tail -n 80 || true"


class AutoCassandra16156(CassandraRawRingProblem):
    """Survivor keeps gossiping to a decommissioned (LEFT), now-unreachable peer — log-spam bug."""

    db_name = "cassandra"
    db_version = "3.11.8"
    cassandra_version = "3.11.8"
    source_git_ref = "cassandra-3.11.8"
    ring_namespace = "cassraw-16156"
    replicas = 2

    root_cause_file = "src/java/org/apache/cassandra/gms/Gossiper.java"
    root_cause_description = (
        "After a node is decommissioned it transitions to LEFT and leaves the ring, but the "
        "surviving peer's Gossiper STILL selects it for gossip through the unreachable-member "
        "path: Gossiper.run()'s periodic GossipTask calls maybeGossipToUnreachableMember(), which "
        "draws an endpoint from the unreachableEndpoints map and calls sendGossip() to it without "
        "excluding endpoints whose STATUS is LEFT (a departed node). Because the LEFT node remains "
        "in the gossip endpoint-state map (normal expiry handling) but its pod / port 7000 is gone, "
        "every selection produces an OutboundTcpConnection connect attempt on the "
        "MessagingService-Outgoing-/<ip>-Gossip thread that fails (java.net.ConnectException), "
        "producing repeated connection-failure log spam targeting the departed node. The fix "
        "(3.11.9 / 3.0.23 / 4.0) stops selecting LEFT/dead-state endpoints for gossip, so after "
        "the node is convicted LEFT/DOWN there are zero further connect attempts. Component: "
        "Cluster/Gossip. NOTE: the verbatim stack trace points at OutboundTcpConnection.java "
        "(connect/newSocket) — that is only the symptom/logging site, not the root cause."
    )

    def _kubectl(self, args: str, timeout: int = 120) -> str:
        cmd = f"kubectl -n {self.app.namespace} {args}"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (res.stdout or "") + (res.stderr or "")

    def _wait_pod_gone(self, pod: str, timeout: int = 120) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self._kubectl(f"get pod {pod} --no-headers --ignore-not-found")
            if not out.strip():
                return True
            time.sleep(4)
        return False

    def _wait_signature(self, timeout: int = 300) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = self.app.exec(_SURVIVOR, _GREP_CMD)
            for ln in out.splitlines():
                if re.search(_SIGNATURE, ln):
                    return ln.strip()
            time.sleep(15)
        return ""

    @mark_fault_injected
    def inject_fault(self):
        """Decommission the victim, enable DEBUG on the survivor, make the victim unreachable."""
        app = self.app
        victim_ip = app.pod_ip(_VICTIM)
        logger.info(f"[16156] survivor={_SURVIVOR} victim={_VICTIM} victim_ip={victim_ip}")

        # STEP 1 — surface the DEBUG connect spam at runtime on the survivor (no restart).
        app.nodetool(_SURVIVOR, "setlogginglevel org.apache.cassandra.net.OutboundTcpConnection DEBUG")
        app.nodetool(_SURVIVOR, "setlogginglevel org.apache.cassandra.gms.Gossiper DEBUG")

        # STEP 2 — decommission the victim so it transitions to LEFT and leaves the ring.
        logger.info("[16156] decommissioning victim cass-1 ...")
        app.nodetool(_VICTIM, "decommission")
        app.wait_ring(1, observer_pod=_SURVIVOR, timeout=300)
        logger.info(f"[16156] post-decommission survivor status:\n{app.nodetool(_SURVIVOR, 'status')}")
        gi = app.nodetool(_SURVIVOR, "gossipinfo")
        left = [ln.strip() for ln in gi.splitlines() if "LEFT" in ln]
        logger.info(f"[16156] survivor gossipinfo LEFT entries: {left}")

        # STEP 3 — make the departed node UNREACHABLE: scale the StatefulSet down so cass-1's
        # pod (and its port 7000) disappear; the survivor still holds the LEFT endpoint and keeps
        # selecting it for gossip.
        logger.info("[16156] scaling StatefulSet to 1 (closing victim pod / port 7000) ...")
        self._kubectl(f"scale statefulset/{app.statefulset_name} --replicas=1")
        self._wait_pod_gone(_VICTIM, timeout=120)

        # STEP 4 — wait for the verbatim gossip-to-a-departed-node connect failure (the first one
        # appears only after the OS connect timeout elapses ~2 min in kind's blackhole net).
        sig = self._wait_signature(timeout=300)
        if sig:
            logger.info(f"[16156] CAPTURED buggy signature: {sig}")
        else:
            logger.warning(
                "[16156] connect-failure signature not present within inject window; "
                "the mitigation oracle will keep polling debug.log"
            )

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=_SURVIVOR,
            pattern=_SIGNATURE,
            source="command",
            command=_GREP_CMD,
            attempts=10,
            retry_delay=20.0,
        )
