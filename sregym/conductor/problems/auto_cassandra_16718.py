"""CASSANDRA-16718: Changing listen_address with prefer_local may lead to issues.

CASSANDRA-16718: Changing listen_address with prefer_local may lead to issues —
reproduced on the raw-ring harness.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16718
Buggy: 4.1.1   ->   Fixed: 4.1.2  (also fixed in 4.0.10, 5.0-alpha1, 5.0)
Component: Local/Config

Reproduction summary (a MULTI-NODE RING scenario — NOT a single fresh node / single CQL):
On a UN/UN ring with ``prefer_local=true`` (GossipingPropertyFileSnitch) and a node whose
internal (listen) address differs from its broadcast address, the seed caches the peer's
INTERNAL_ADDRESS_AND_PORT (its pod IP) as ``preferred_ip`` in ``system.peers`` and routes
outbound gossip there. Delete cass-1 and recreate it so it returns with a NEW listen_address
(new pod IP) but the SAME broadcast endpoint (a stable per-pod ClusterIP): the seed retains
the STALE ``preferred_ip`` (the old, now-dead internal IP), so the recreated node's startup
gossip shadow round never receives the seed's reply and ``Gossiper.doShadowRound`` throws.

HOW THE RAW-RING HARNESS EXPRESSES IT (3 ingredients a single CQL ``reproducer`` cannot):
(1) a STABLE broadcast that survives pod recreation while listen_address changes — provided by
a per-pod ClusterIP ``Service`` used as ``CASSANDRA_BROADCAST_ADDRESS`` with listen_address =
pod IP (so internal != broadcast and ``prefer_local`` caches a non-null pod IP);
(2) ``prefer_local=true`` injected into ``cassandra-rackdc.properties`` on each node; and
(3) a delete+recreate of cass-1 that changes its pod IP (listen) but keeps its ClusterIP
(broadcast). ``deploy_app`` (``post_deploy``) creates the two ClusterIP services + two bare
pods and forms the UN/UN ring; ``inject_fault`` performs the delete+recreate trigger; the
failing node runs as an in-pod CassandraDaemon (its abort log persists in the still-alive
``tail -f /dev/null`` pod) so the ``CassandraLogGrepOracle`` can read it.

Verbatim buggy signature (cass-1, cassandra:4.1.1; startup-abort log):
  Exception (java.lang.RuntimeException) encountered during startup: Unable to gossip with any peers
  java.lang.RuntimeException: Unable to gossip with any peers
      at org.apache.cassandra.gms.Gossiper.doShadowRound(Gossiper.java:1916)
      at org.apache.cassandra.service.StorageService.checkForEndpointCollision(StorageService.java:694)
      at org.apache.cassandra.service.StorageService.prepareToJoin(StorageService.java:996)
      at org.apache.cassandra.service.StorageService.initServer(StorageService.java:842)
      at org.apache.cassandra.service.StorageService.initServer(StorageService.java:775)
      at org.apache.cassandra.service.CassandraDaemon.setup(CassandraDaemon.java:425)
      at org.apache.cassandra.service.CassandraDaemon.activate(CassandraDaemon.java:752)
      at org.apache.cassandra.service.CassandraDaemon.main(CassandraDaemon.java:876)
A/B control on fixed 4.1.2 (identical topology + trigger): ``doShadowRound`` SUCCEEDS (the fix
purges the stale internal address and resolves the current one), so startup proceeds PAST the
shadow round and only stops at the unrelated endpoint-collision guard
(StorageService.java:784 — "A node with address ... already exists"). The load-bearing contrast
is ``doShadowRound`` THROWING on 4.1.1 vs RETURNING on 4.1.2. See the authoritative evidence
log: .claude/repro-evidence/repro-CASSANDRA-16718.md
"""

import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.service.apps.cassandra_raw_ring import _run
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)

# Verbatim startup-abort the buggy 4.1.1 node dies with inside the gossip shadow round.
_ABORT_PATTERN = r"Unable to gossip with any peers"


class AutoCassandra16718(CassandraRawRingProblem):
    """seed ``cass16718-0`` + ``cass16718-1`` (recreated with a new pod IP, stale preferred_ip).

    Realised through the benchmark architecture: ``deploy_app`` (``post_deploy``) creates two
    per-pod ClusterIP services (stable broadcast) and two ``tail -f /dev/null`` pods on the
    stock ``cassandra:4.1.1`` image, sets ``prefer_local=true`` on each, launches their in-pod
    CassandraDaemons to form a UN/UN ring, and confirms the seed cached cass-1's pod IP as a
    non-null ``preferred_ip`` (internal != broadcast). ``inject_fault`` deletes cass-1 and
    recreates it with a NEW pod IP but the SAME broadcast ClusterIP; the seed keeps the STALE
    ``preferred_ip`` (the dead old pod IP), so the recreated cass-1's startup shadow round
    never gets the seed's reply and aborts with ``RuntimeException: Unable to gossip with any
    peers`` at ``Gossiper.doShadowRound``. The ``CassandraLogGrepOracle`` greps cass-1's
    persistent in-pod startup log for that line. (Fixed 4.1.2 resolves the current address and
    joins past the shadow round.)
    """

    db_name = "cassandra"
    db_version = "4.1.1"
    cassandra_version = "4.1.1"
    source_git_ref = "cassandra-4.1.1"
    ring_namespace = "cassraw-16718"
    # All nodes are bespoke bare pods fronted by per-pod ClusterIP services (stable broadcast
    # address that survives pod recreation). The StatefulSet ring is therefore unused — set
    # replicas=0 so deploy() stands up only the (empty) StatefulSet + headless Service, and
    # post_deploy() builds the real per-pod-ClusterIP topology.
    replicas = 0
    num_tokens = 16

    root_cause_file = "src/java/org/apache/cassandra/locator/ReconnectableSnitchHelper.java"
    root_cause_description = (
        "Changing listen_address with prefer_local enabled may lead to issues. With a "
        "reconnectable snitch (prefer_local=true), Cassandra caches each peer's "
        "INTERNAL_ADDRESS_AND_PORT as preferred_ip in system.peers and routes outbound gossip "
        "there (OutboundConnectionSettings used SystemKeyspace.getPreferredIP). When a node keeps "
        "a stable broadcast/gossip identity but its internal (listen) address changes, the seed "
        "still routes to the OLD internal address, so the startup gossip shadow round reply never "
        "reaches the node and Gossiper.doShadowRound throws 'Unable to gossip with any peers'. "
        "The fix makes ReconnectableSnitchHelper.onDead() purge the stale INTERNAL_ADDRESS_AND_PORT "
        "and close the outbound connection, and resolves OutboundConnectionSettings via "
        "Gossiper.getInternalAddressAndPort instead of the cached preferred_ip."
    )

    _SEED = "cass16718-0"
    _NODE = "cass16718-1"
    _LOG = "/var/log/cassandra/node.log"

    # ── topology helpers (per-pod ClusterIP broadcast + prefer_local) ────────────────

    def _service_yaml(self, name: str, node_label: str) -> str:
        return (
            "apiVersion: v1\n"
            "kind: Service\n"
            f"metadata: {{ name: {name}, namespace: {self.ring_namespace} }}\n"
            "spec:\n"
            "  type: ClusterIP\n"
            f"  selector: {{ app: c16718, node: {node_label} }}\n"
            "  ports:\n"
            "    - { name: intra, port: 7000, targetPort: 7000 }\n"
            "    - { name: cql, port: 9042, targetPort: 9042 }\n"
        )

    def _pod_yaml(self, name: str, node_label: str, broadcast: str, seed_ip: str) -> str:
        return (
            "apiVersion: v1\n"
            "kind: Pod\n"
            f"metadata: {{ name: {name}, namespace: {self.ring_namespace}, "
            f"labels: {{ app: c16718, node: {node_label} }} }}\n"
            "spec:\n"
            "  terminationGracePeriodSeconds: 10\n"
            "  containers:\n"
            "    - name: cassandra\n"
            f"      image: {self.image}\n"
            "      imagePullPolicy: IfNotPresent\n"
            "      ports: [ {containerPort: 9042}, {containerPort: 7000} ]\n"
            "      env:\n"
            f'        - {{ name: CASSANDRA_SEEDS, value: "{seed_ip}" }}\n'
            f'        - {{ name: CASSANDRA_BROADCAST_ADDRESS, value: "{broadcast}" }}\n'
            '        - { name: CASSANDRA_CLUSTER_NAME, value: "repro" }\n'
            '        - { name: CASSANDRA_ENDPOINT_SNITCH, value: "GossipingPropertyFileSnitch" }\n'
            f'        - {{ name: CASSANDRA_NUM_TOKENS, value: "{self.num_tokens}" }}\n'
            '        - { name: CASSANDRA_DC, value: "dc1" }\n'
            '        - { name: CASSANDRA_RACK, value: "rack1" }\n'
            '      command: ["bash","-c"]\n'
            '      args: ["tail -f /dev/null"]\n'
            "      volumeMounts:\n"
            "        - { name: data, mountPath: /var/lib/cassandra }\n"
            "  volumes:\n"
            "    - { name: data, emptyDir: {} }\n"
        )

    def _cluster_ip(self, name: str) -> str:
        return (
            _run(f"kubectl get svc -n {self.ring_namespace} {name} -o jsonpath='{{.spec.clusterIP}}'")
            .stdout.strip()
            .strip("'")
        )

    def _launch(self, pod: str):
        """Inject prefer_local into cassandra-rackdc.properties, then start the in-pod daemon."""
        self.app.exec(
            pod,
            "sed -i '/prefer_local/d' /etc/cassandra/cassandra-rackdc.properties; "
            "echo 'prefer_local=true' >> /etc/cassandra/cassandra-rackdc.properties",
        )
        self.app.launch_daemon(pod, log_file=self._LOG)

    def _wait_log(self, pod: str, markers: tuple[str, ...], timeout: int) -> str:
        """Poll a pod's launch log until any marker substring appears; return the matched line."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.app.exec(pod, f"cat {self._LOG} 2>/dev/null || true")
            for ln in text.splitlines():
                if any(m in ln for m in markers):
                    return ln.strip()
            time.sleep(4)
        return ""

    def post_deploy(self):
        app = self.app
        # 1. Per-pod ClusterIP services give each node a STABLE broadcast address (survives pod
        #    recreation) distinct from its changing listen_address (pod IP).
        logger.info("[16718] post_deploy: create per-pod ClusterIP services svc-0/svc-1")
        _run("kubectl apply -f -", input=self._service_yaml("svc-0", "s0"), check=True)
        _run("kubectl apply -f -", input=self._service_yaml("svc-1", "s1"), check=True)
        svc0 = self._cluster_ip("svc-0")
        svc1 = self._cluster_ip("svc-1")
        logger.info(f"[16718] svc-0(seed broadcast)={svc0}  svc-1(cass-1 broadcast)={svc1}")

        # 2. Two bare pods (broadcast = own ClusterIP, listen = pod IP), seeds = svc-0.
        _run("kubectl apply -f -", input=self._pod_yaml(self._SEED, "s0", svc0, svc0), check=True)
        _run("kubectl apply -f -", input=self._pod_yaml(self._NODE, "s1", svc1, svc0), check=True)
        app.wait_pod_running(self._SEED, timeout=240)
        app.wait_pod_running(self._NODE, timeout=240)

        # 3. Launch the seed daemon (prefer_local), wait UN; then cass-1, wait UN/UN.
        logger.info("[16718] launch seed daemon (prefer_local), wait 1x UN")
        self._launch(self._SEED)
        app.wait_ring(1, observer_pod=self._SEED, timeout=420)
        logger.info("[16718] launch cass-1 daemon (prefer_local), wait 2x UN")
        self._launch(self._NODE)
        app.wait_ring(2, observer_pod=self._SEED, timeout=420)

        # 4. Confirm the precondition that arms the bug: internal (pod IP) != broadcast
        #    (ClusterIP) and the seed cached cass-1's pod IP as a non-null preferred_ip.
        peers = app.cqlsh(self._SEED, "SELECT peer, preferred_ip FROM system.peers;")
        logger.info(f"[16718] post_deploy seed system.peers (preferred_ip should be cass-1 pod IP):\n{peers.strip()}")

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        svc1 = self._cluster_ip("svc-1")
        old_ip = app.pod_ip(self._NODE)
        logger.info(f"[16718] inject_fault: cass-1 current pod IP (listen)={old_ip}, broadcast(svc-1)={svc1}")

        # STEP 1 — delete cass-1; the buggy seed keeps the STALE preferred_ip (the old pod IP).
        logger.info("[16718] STEP1 delete cass-1 (kill daemon + pod)")
        app.exec(self._NODE, "pkill -9 -f CassandraDaemon || true")
        _run(f"kubectl delete pod {self._NODE} -n {self.ring_namespace} --grace-period=5 --wait=true")
        stale = app.cqlsh(self._SEED, "SELECT peer, preferred_ip FROM system.peers;")
        logger.info(f"[16718] STEP1 seed retains stale preferred_ip after delete:\n{stale.strip()}")

        # STEP 2 — recreate cass-1: SAME broadcast ClusterIP (same label -> svc-1), NEW pod IP.
        logger.info("[16718] STEP2 recreate cass-1 (same svc-1 broadcast, new pod IP)")
        _run("kubectl apply -f -", input=self._pod_yaml(self._NODE, "s1", svc1, self._cluster_ip("svc-0")), check=True)
        app.wait_pod_running(self._NODE, timeout=240)
        new_ip = app.pod_ip(self._NODE)
        logger.info(f"[16718] STEP2 cass-1 new pod IP (listen)={new_ip} (was {old_ip}); broadcast unchanged={svc1}")

        # STEP 3 — launch the recreated cass-1; on 4.1.1 the seed mis-routes the shadow-round
        # reply to the dead old pod IP, so doShadowRound times out and startup aborts.
        logger.info("[16718] STEP3 launch recreated cass-1; wait for shadow-round startup abort")
        self._launch(self._NODE)
        captured = self._wait_log(
            self._NODE,
            ("Unable to gossip with any peers", "encountered during startup"),
            timeout=240,
        )
        if captured:
            logger.info(f"[16718] inject_fault captured buggy startup-abort signature:\n{captured}")
        else:
            logger.warning("[16718] startup-abort signature not observed within 240s")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._NODE,
            source="command",
            command=f"cat {self._LOG} 2>/dev/null || true",
            pattern=_ABORT_PATTERN,
            attempts=4,
            retry_delay=10.0,
        )
