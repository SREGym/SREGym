"""CASSANDRA-16146: Node state incorrectly set to NORMAL after disablegossip+enablegossip during bootstrap.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16146
Buggy: 3.11.9  ->  Fixed: 3.11.10 (A/B control = cassandra:3.11.10).
Component: Cluster/Gossip

THE BUG (2-node ring; a seed at NORMAL plus a joiner parked in BOOT/JOINING):
  ``StorageService#setGossipTokens`` sets the gossip STATUS to NORMAL blindly. A node parked in
  BOOT/JOINING (its bootstrap halted, or — as reproduced here — it is held in
  ``-Dcassandra.write_survey=true``, which completes streaming then deliberately stops short of
  becoming an active ring member: "Startup complete, but write survey mode is active, not becoming
  an active ring member"; tokens are saved so ``getLocalTokens()`` is non-empty) is flipped to
  NORMAL when an operator runs ``nodetool disablegossip`` then ``nodetool enablegossip``: the
  ``enablegossip`` -> ``startGossiping()`` path calls ``setGossipTokens()``, which overrides the
  real BOOT gossip state with NORMAL. The node then advertises STATUS=NORMAL while its operation
  mode is still JOINING, so the rest of the ring treats a never-joined node as a Normal member.
  The 3.11.10 fix adds an ``isNormal()`` guard in ``stopGossiping()`` that REFUSES ``disablegossip``
  on a non-NORMAL node ("Unable to stop gossip because the node is not in the normal state").

VERBATIM BUGGY SIGNATURE (literal copy from the joiner's gossip self-entry after the dance):
  STATUS:87:NORMAL,-1077568207160367180        (was BOOT)
while its own operation mode is still ``Mode: JOINING`` and the seed's ``nodetool status``
consequently flips the joiner from ``UJ`` to ``UN``.

HOW THE RAW-RING HARNESS MAKES THIS RUNNABLE (the former blockers, resolved):
  * Topology — ``CassandraRawRingApplication`` deploys ``seed`` = cass-0 (StatefulSet replicas=1, the
    NORMAL ring member) plus a bare ``joiner`` pod whose container command is ``tail -f /dev/null``.
    Cassandra runs inside ``joiner`` as a launched PROCESS (``launch_daemon``) so its per-process
    ``JVM_EXTRA_OPTS`` can carry ``-Dcassandra.write_survey=true`` while the seed runs unmodified —
    the heterogeneous 2-pod precondition a uniform StatefulSet cannot express.
  * Trigger / detection — the fault is driven by ``nodetool`` (disablegossip/enablegossip) and
    observed via ``nodetool gossipinfo`` / ``nodetool status``, not a CQL result. The mitigation
    oracle (``CassandraLogGrepOracle``, source=command) extracts the joiner's OWN gossip STATUS line
    (scoped to its broadcast address) and greps for ``STATUS:<gen>:NORMAL``: present on buggy 3.11.9
    after the dance; on fixed 3.11.10 ``disablegossip`` is refused so the joiner stays ``BOOT``.

Verified end-to-end on kind-fleet1: the write_survey joiner parks at ``STATUS:22:BOOT`` / ``Mode:
JOINING`` (seed sees ``UJ``); after ``disablegossip``+``enablegossip`` its self-entry flips to
``STATUS:106:NORMAL`` while it is still ``Mode: JOINING`` and the seed flips it ``UJ`` -> ``UN``.
"""

import logging
import re
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra16146(CassandraRawRingProblem):
    """seed cass-0 (NORMAL) + bare ``joiner`` parked in write_survey BOOT/JOINING, flipped to NORMAL."""

    db_name = "cassandra"
    db_version = "3.11.9"
    cassandra_version = "3.11.9"
    source_git_ref = "cassandra-3.11.9"
    ring_namespace = "cassraw-16146"
    # seed = cass-0 (the NORMAL ring member).
    replicas = 1
    # joiner = bare pod parked at `tail -f /dev/null`; cassandra is launched in-pod with
    # -Dcassandra.write_survey=true so only the joiner gets that flag.
    extra_pods = [{"pod_name": "joiner", "command": "tail -f /dev/null"}]

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "StorageService#setGossipTokens sets the gossip STATUS to NORMAL blindly. When an operator "
        "stops and re-starts gossip (nodetool disablegossip + enablegossip) on a node still in "
        "BOOT/JOINING — e.g. a node whose bootstrap halted or that is in write_survey mode — "
        "startGossiping() calls setGossipTokens(), which overrides the actual BOOT gossip state with "
        "NORMAL. The node then advertises STATUS=NORMAL while its operation mode is still JOINING, so "
        "the rest of the ring treats a never-joined node as a Normal member eligible for reads/writes. "
        "The 3.11.10 fix adds an isNormal() guard in stopGossiping() that rejects disablegossip unless "
        "the node is in the NORMAL state."
    )

    _SEED = "cass-0"
    _JOINER = "joiner"
    _JOINER_LOG = "/var/log/cassandra/joiner.log"
    # Extract the joiner's OWN gossip STATUS line, scoped to its broadcast address (the pod IP), so a
    # match cannot accidentally pick up the seed's STATUS:NORMAL. Prints e.g.
    #   STATUS:106:NORMAL,-2024520439209723891   (buggy, after the dance)  or
    #   STATUS:22:BOOT,4101405499401247230       (fixed / before the dance).
    _SELF_STATUS_CMD = (
        "IP=$(hostname -i | awk '{print $1}'); "
        'nodetool gossipinfo | awk -v ip="/$IP" '
        "'index($0,ip)==1{f=1;next} /^\\//{f=0} f&&/STATUS:/{print; exit}'"
    )
    _NORMAL_PATTERN = r"STATUS:\d+:NORMAL"

    def _wait_joiner_log(self, markers: tuple[str, ...], timeout: int) -> str:
        """Poll the joiner's launch log until any marker substring appears; return the matched line."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.app.exec(self._JOINER, f"cat {self._JOINER_LOG} 2>/dev/null || true")
            for ln in text.splitlines():
                if any(m in ln for m in markers):
                    return ln.strip()
            time.sleep(4)
        return ""

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        joiner_ip = app.pod_ip(self._JOINER)
        logger.info(f"[16146] joiner_ip={joiner_ip}; seed={self._SEED}")

        # STEP 1 — launch cassandra on the joiner in write_survey mode; it streams from the seed then
        # parks before joining ("Startup complete, but write survey mode is active ...").
        logger.info("[16146] STEP1 launch write_survey joiner daemon, wait for the parked state")
        app.launch_daemon(self._JOINER, jvm_extra_opts="-Dcassandra.write_survey=true", log_file=self._JOINER_LOG)
        parked = self._wait_joiner_log(("write survey mode is active",), timeout=360)
        logger.info(f"[16146] STEP1 parked marker: {parked!r}")

        # STEP 2 — confirm the BOOT/JOINING precondition (seed sees UJ; joiner self STATUS=BOOT).
        app.wait_node_state(self._SEED, joiner_ip, "UJ", timeout=180)
        before_status = app.exec(self._JOINER, self._SELF_STATUS_CMD).strip()
        before_mode = app.exec(self._JOINER, "nodetool netstats | head -1").strip()
        logger.info(
            f"[16146] STEP2 BEFORE dance: joiner self {before_status!r}; {before_mode!r}; "
            f"seed sees joiner = {app.node_state(self._SEED, joiner_ip)!r}"
        )

        # STEP 3 — THE DANCE (both succeed on buggy 3.11.9; the 3.11.10 fix REFUSES disablegossip).
        logger.info("[16146] STEP3 disablegossip + enablegossip on joiner")
        dis = app.nodetool(self._JOINER, "disablegossip")
        if "not in the normal state" in dis:
            logger.warning(f"[16146] STEP3 disablegossip REFUSED (fixed-binary behaviour): {dis.strip()}")
        app.enablegossip(self._JOINER)

        # STEP 4 — observe the flip: joiner self gossip STATUS BOOT -> NORMAL while Mode stays JOINING,
        # and the seed flips the joiner UJ -> UN.
        captured, st = "", ""
        deadline = time.time() + 120
        while time.time() < deadline:
            st = app.exec(self._JOINER, self._SELF_STATUS_CMD).strip()
            if re.search(self._NORMAL_PATTERN, st):
                captured = st
                break
            time.sleep(5)
        after_mode = app.exec(self._JOINER, "nodetool netstats | head -1").strip()
        seed_sees = app.node_state(self._SEED, joiner_ip)
        if captured:
            logger.info(
                f"[16146] inject_fault captured buggy signature: joiner {captured!r} "
                f"while {after_mode!r}; seed sees joiner = {seed_sees!r}"
            )
        else:
            logger.warning(
                f"[16146] BOOT->NORMAL flip not observed within 120s "
                f"(joiner self status={st!r}, {after_mode!r}, seed sees={seed_sees!r})"
            )

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._JOINER,
            source="command",
            command=self._SELF_STATUS_CMD,
            pattern=self._NORMAL_PATTERN,
            attempts=4,
            retry_delay=10.0,
        )
