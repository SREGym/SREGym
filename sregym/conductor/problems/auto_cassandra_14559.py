"""CASSANDRA-14559 — FatClient endpoint-collision, reproduced on the raw-ring harness.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14559
Buggy: 3.11.7  ->  Fixed: 3.11.8 (A/B control = cassandra:3.11.8).
Components: Consistency / Bootstrap and Decommission

THE BUG (2-node ring; a surviving peer does the FatClient conviction, the replaced node keeps a
STABLE same address):
  A node is replaced via ``-Dcassandra.replace_address=<its own IP>`` (the same-address HIBERNATE
  path), killed mid-bootstrap so a ``STATUS:hibernate,true`` gossip entry is left on the surviving
  peer, then wiped and restarted WITHOUT the replace_address flag. On 3.11.7 this no-flag restart
  is ALLOWED (no collision check) and begins a fresh bootstrap with new tokens; killing it
  mid-bootstrap a second time makes the surviving peer convict it as a FatClient ~30s later,
  unsafely removing the endpoint AND its tokens from gossip. The 3.11.8 fix adds a
  hibernate-collision check in ``checkForEndpointCollision`` that REFUSES the no-flag restart
  ("A node with address ... already exists, cancelling join").

VERBATIM BUGGY SIGNATURE (literal copy from the surviving peer's system.log):
  INFO  [GossipTasks:1] Gossiper.java:880 - FatClient /<TARGET_IP> has been silent for 30000ms, removing from gossip

HOW THE RAW-RING HARNESS MAKES THIS RUNNABLE (the former blockers, resolved):
  * Topology — ``CassandraRawRingApplication`` deploys ``seed`` = cass-0 (StatefulSet replicas=1,
    the surviving peer that does the conviction) plus a bare ``target`` pod whose container command
    is ``tail -f /dev/null``. Cassandra runs inside ``target`` as a launched/killed PROCESS
    (``launch_daemon`` / ``kill_daemon``) while the pod — and its IP — stay STABLE across in-pod
    restarts. That stable IP IS the "replace with the same address" precondition.
  * In-pod process control + data wipe — ``launch_daemon`` (with per-phase JVM_EXTRA_OPTS),
    ``kill_daemon`` (pkill CassandraDaemon), ``wipe_data`` (rm data/commitlog/hints/saved_caches).
  * ``-Dcassandra.ring_delay_ms=60000`` on the target launches widens the mid-bootstrap kill window;
    the seed keeps its default ~30s ring delay so its FatClient timer fires at 30000ms (matching the
    verbatim signature).
  * Detection — the only signal is a server-log line on the SEED, so CassandraLogGrepOracle greps
    cass-0's persistent ``/var/log/cassandra/system.log`` for the FatClient line.

Verified end-to-end on kind-fleet4: STEP4 leaves ``STATUS:2:hibernate,true`` on the seed for the
target endpoint; STEP5 (no-flag relaunch) is ALLOWED on 3.11.7 and the seed flips the target to UJ;
~30s after STEP6 the seed logs ``FatClient /<TARGET_IP> has been silent for 30000ms, removing from
gossip`` (on 3.11.8 STEP5 would instead be refused with "already exists, cancelling join").
"""

import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra14559(CassandraRawRingProblem):
    """seed cass-0 (surviving peer) + bare `target` whose in-pod CassandraDaemon is replaced/killed."""

    cassandra_version = "3.11.7"
    source_git_ref = "cassandra-3.11.7"
    ring_namespace = "cassraw-14559"
    # seed = cass-0 (the surviving peer that convicts the FatClient).
    replicas = 1
    # target = bare pod parked at `tail -f /dev/null`; cassandra is launched/killed in-pod so its
    # IP stays stable (the same-address replace precondition).
    extra_pods = [{"pod_name": "target", "command": "tail -f /dev/null"}]

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "checkForEndpointCollision() does not reject a node that restarts WITHOUT "
        "cassandra.replace_address when a gossip entry for the same broadcast address is "
        "already in the HIBERNATE state (left behind by an interrupted same-address replace). "
        "On 3.11.7 such a no-flag restart is allowed and begins a fresh bootstrap with new "
        "tokens; if it is killed mid-bootstrap, the surviving peer convicts it as a FatClient "
        "~30s later and unsafely removes the endpoint and its tokens from gossip. The 3.11.8 "
        "fix adds a hibernate-collision check (Gossiper.java:825 warning) that throws a "
        "RuntimeException 'A node with address ... already exists, cancelling join. Use "
        "cassandra.replace_address if you want to replace this node.' to block the unsafe path."
    )

    _SEED = "cass-0"
    _TARGET = "target"
    _RING_DELAY_MS = 60000
    _FAT_PATTERN = r"FatClient /\S+ has been silent for \d+ms, removing from gossip"

    def _target_jvm(self, *extra: str) -> str:
        opts = [f"-Dcassandra.ring_delay_ms={self._RING_DELAY_MS}", *extra]
        return " ".join(opts)

    def _wait_target_log(self, log_file: str, markers: tuple[str, ...], timeout: int) -> str:
        """Poll target's launch log until any marker substring appears; return the matched line."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.app.exec(self._TARGET, f"cat {log_file} 2>/dev/null || true")
            for ln in text.splitlines():
                if any(m in ln for m in markers):
                    return ln.strip()
            time.sleep(4)
        return ""

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        target_ip = app.pod_ip(self._TARGET)
        logger.info(f"[14559] target_ip={target_ip}; seed={self._SEED}")

        # STEP 1 — launch cassandra on target so it joins normally (seed sees UN).
        logger.info("[14559] STEP1 launch target daemon, wait UN")
        app.launch_daemon(self._TARGET, jvm_extra_opts=self._target_jvm(), log_file="/var/log/cassandra/s1.log")
        if not app.wait_node_state(self._SEED, target_ip, "UN", timeout=240):
            logger.warning("[14559] STEP1: seed did not see target UN within 240s")

        # STEP 2 — kill target, wait until seed marks it DN.
        logger.info("[14559] STEP2 kill target daemon, wait DN")
        app.kill_daemon(self._TARGET)
        if not app.wait_node_state(self._SEED, target_ip, "DN", timeout=120):
            logger.warning("[14559] STEP2: seed did not see target DN within 120s")

        # STEP 3 — wipe + relaunch with replace_address=own IP (same-address HIBERNATE path).
        logger.info("[14559] STEP3 wipe + relaunch with replace_address=own IP")
        app.wipe_data(self._TARGET)
        app.launch_daemon(
            self._TARGET,
            jvm_extra_opts=self._target_jvm(f"-Dcassandra.replace_address={target_ip}"),
            log_file="/var/log/cassandra/s3.log",
        )
        marker = self._wait_target_log(
            "/var/log/cassandra/s3.log", ("ready to bootstrap", "calculation complete"), timeout=180
        )
        logger.info(f"[14559] STEP3 marker: {marker!r}")

        # STEP 4 — kill mid-bootstrap; the seed now holds a STATUS:hibernate,true entry for target.
        logger.info("[14559] STEP4 kill target mid-bootstrap (replace)")
        app.kill_daemon(self._TARGET)
        gi = app.exec(
            self._SEED,
            f"nodetool gossipinfo 2>/dev/null | grep -A12 {target_ip} | grep -iE 'STATUS|hibernate' | head -3 || true",
        )
        logger.info(f"[14559] STEP4 seed gossip for target:\n{gi.strip()}")

        # STEP 5 — wipe + relaunch WITHOUT the replace flag (3.11.7 ALLOWS; 3.11.8 REFUSES).
        logger.info("[14559] STEP5 wipe + relaunch WITHOUT replace flag")
        app.wipe_data(self._TARGET)
        app.launch_daemon(self._TARGET, jvm_extra_opts=self._target_jvm(), log_file="/var/log/cassandra/s5.log")
        refused = self._wait_target_log("/var/log/cassandra/s5.log", ("cancelling join", "already exists"), timeout=30)
        if refused:
            logger.warning(f"[14559] STEP5 REFUSED (fixed-binary behaviour): {refused}")
        else:
            app.wait_node_state(self._SEED, target_ip, "UJ", timeout=90)
            logger.info(f"[14559] STEP5 seed sees target = {app.node_state(self._SEED, target_ip)!r}")

        # STEP 6 — kill mid-bootstrap again (during the pending-range sleep).
        logger.info("[14559] STEP6 kill target mid-bootstrap again")
        app.kill_daemon(self._TARGET)

        # STEP 7 — ~30s later the seed convicts the endpoint as a FatClient.
        logger.info("[14559] STEP7 wait for seed FatClient conviction")
        captured = ""
        deadline = time.time() + 120
        while time.time() < deadline:
            matches = app.grep_log(self._SEED, self._FAT_PATTERN, source="system_log")
            if matches:
                captured = matches[-1].strip()
                break
            time.sleep(5)
        if captured:
            logger.info(f"[14559] inject_fault captured buggy FatClient signature:\n{captured}")
        else:
            logger.warning("[14559] FatClient signature not observed within 120s")

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._SEED,
            source="system_log",
            pattern=self._FAT_PATTERN,
            attempts=6,
            retry_delay=10.0,
        )
