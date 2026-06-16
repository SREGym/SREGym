"""CASSANDRA-14463 — replace_address startup-guard bug, reproduced on the raw-ring harness.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-14463
Buggy: 4.0.0.  Fixed: 4.0.1 (A/B control = cassandra:4.0.1).

THE BUG (config-gated, SINGLE self-seeded node, startup guard):
  Start a node with ``-Dcassandra.replace_address=<ip>`` while the node is in its OWN seed list
  (the stock docker-entrypoint makes a lone pod its own seed:
  ``: ${CASSANDRA_SEEDS:="$CASSANDRA_BROADCAST_ADDRESS"}``), with auto_bootstrap=true and
  initial_token unset. On 4.0.0 the seed+replace combination is NOT blocked at startup — the node
  proceeds into ``prepareForReplacement()`` and (with a real dead-node IP) would skip streaming and
  generate a fresh random token set instead of inheriting the dead node's tokens. On 4.0.1 the node
  REFUSES to start in this configuration unless ``-Dcassandra.allow_unsafe_replace=true``.

  The discriminating code change is ``StorageService.prepareForReplacement()``:
    - 4.0.0 guard: ``if (!isAutoBootstrap() && !allow_unsafe_replace) throw ...``  — not taken when
      auto_bootstrap=true (the default), so a seed proceeds into replacement logic.
    - 4.0.1 guard: ``if (!shouldBootstrap() && !allow_unsafe_replace) throw ...`` where
      ``shouldBootstrap() = isAutoBootstrap() && !bootstrapComplete() && !isSeed()``, so a seed
      (isSeed()==true => shouldBootstrap()==false) is blocked.

VERBATIM BUGGY SIGNATURE — cassandra:4.0.0 PASSES the seed+replace guard and reaches replacement:
  INFO  [main] StorageService.java:528 - Gathering node replacement information for /10.255.255.254:7000
  (then dies downstream ONLY because the dummy target isn't in gossip:
   "Cannot replace_address /10.255.255.254:7000 because it doesn't exist in gossip").
On 4.0.1 that line appears 0 times — the node refuses AT the guard (StorageService.java:522):
  "Replacing a node without bootstrapping risks invalidating consistency guarantees ... To perform
   this operation, please restart with -Dcassandra.allow_unsafe_replace=true".

HOW THE RAW-RING HARNESS MAKES THIS RUNNABLE (the three former blockers, resolved):
  1. The only discriminator is a server-log line → CassandraLogGrepOracle greps the pod's stdout
     (cassandra logs to stdout in the docker image) for "Gathering node replacement information".
     The pod crash-loops, so the oracle reads current + previous container logs and retries across
     a few restarts (each boot re-logs the line before crashing) — robust to restart timing. A dummy
     dead IP (10.255.255.254) is sufficient: the line is present on 4.0.0 and absent on 4.0.1, so no
     real dead node / multi-node orchestration is required to discriminate.
  2. The trigger is a JVM system property: CassandraRawRingApplication launches the node as a bare
     pod whose ``JVM_EXTRA_OPTS`` carries ``-Dcassandra.replace_address=...`` — no operator CR, no
     image-only swap.
  3. The guard fires only when the node is its own seed: ``apply_bare_pod(..., set_seeds=False)``
     omits CASSANDRA_SEEDS so the stock entrypoint self-seeds the pod (isSeed()==true). There is no
     cass-operator seed service to override it.

So this is no longer a stub: ``replicas=0`` (no StatefulSet ring is needed — the fault IS the single
self-seeded replace_address node), ``inject_fault`` launches that node and confirms the buggy log
line, and the CassandraLogGrepOracle grades whether the signature is present (buggy) or absent
(fixed). Verified on kind-fleet4: 4.0.0 emits the line; 4.0.1 instead refuses with the consistency
guard message.
"""

import logging
import time

from sregym.conductor.oracles.cassandra_raw_ring_oracles import CassandraLogGrepOracle
from sregym.conductor.problems.cassandra_raw_ring import CassandraRawRingProblem
from sregym.utils.decorators import mark_fault_injected

logger = logging.getLogger(__name__)


class AutoCassandra14463(CassandraRawRingProblem):
    """Single self-seeded node started with -Dcassandra.replace_address (4.0.0 passes the guard)."""

    cassandra_version = "4.0.0"
    source_git_ref = "cassandra-4.0.0"
    ring_namespace = "cassraw-14463"
    # No ring: the fault is one crash-looping self-seeded node launched in inject_fault.
    replicas = 0

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "In StorageService.prepareForReplacement(), Cassandra 4.0.0 guards the unsafe "
        "non-bootstrapping replacement path with `if (!DatabaseDescriptor.isAutoBootstrap() && "
        "!cassandra.allow_unsafe_replace) throw ...`. Because auto_bootstrap defaults to true, the "
        "guard is not taken even when the replacing node is itself a seed (isSeed()==true), so the "
        "node enters replacement logic; with a real dead-node IP it then skips streaming (a seed "
        "does not bootstrap) and generates a fresh random token set instead of inheriting the dead "
        "node's tokens, silently joining the ring with wrong tokens. The fix (4.0.1) changes the "
        "guard to `if (!shouldBootstrap() && !cassandra.allow_unsafe_replace) throw ...`, where "
        "shouldBootstrap() = isAutoBootstrap() && !bootstrapComplete() && !isSeed(), so a seed is "
        "blocked from replacing without -Dcassandra.allow_unsafe_replace=true."
    )

    _DUMMY_IP = "10.255.255.254"
    _POD = "cass-replace"
    _PATTERN = r"Gathering node replacement information"

    @mark_fault_injected
    def inject_fault(self):
        app = self.app
        jvm = f"{app._jvm_opts()} -Dcassandra.replace_address={self._DUMMY_IP}".strip()
        logger.info(f"[14463] Launching self-seeded node {self._POD} with JVM_EXTRA_OPTS={jvm!r}")
        # set_seeds=False -> stock entrypoint self-seeds the lone pod (isSeed()==true), the
        # precondition for the buggy guard to be reachable.
        app.apply_bare_pod(self._POD, env={"JVM_EXTRA_OPTS": jvm}, set_seeds=False)
        app.wait_pod_running(self._POD, timeout=240)

        # Poll the (crash-looping) pod's logs until the discriminating startup line appears, and
        # record it verbatim as the captured signature. On buggy 4.0.0 it shows within ~30-40s.
        deadline = time.time() + 180
        captured = ""
        while time.time() < deadline:
            text = app.pod_logs_all(self._POD)
            hits = [ln for ln in text.splitlines() if self._PATTERN in ln]
            if hits:
                captured = hits[0].strip()
                break
            time.sleep(8)

        if captured:
            logger.info(f"[14463] inject_fault captured buggy startup signature:\n{captured}")
        else:
            logger.warning(
                "[14463] inject_fault did not observe the buggy line within 180s "
                "(expected on a fixed 4.0.1 binary, which refuses at the guard)."
            )

    def build_mitigation_oracle(self):
        return CassandraLogGrepOracle(
            problem=self,
            pod=self._POD,
            source="pod_logs",
            pattern=self._PATTERN,
            attempts=8,
            retry_delay=10.0,
        )
