"""CASSANDRA-16418: Unsafe to run nodetool cleanup during bootstrap or decommission.

JIRA: https://issues.apache.org/jira/browse/CASSANDRA-16418

Buggy: 4.1.0  ->  Fixed: 4.1.1 (fix commit 8bb9c72f582de6bcc39522ba9ade91fd5bc22f67;
also 4.0.8, 5.0-alpha1, 5.0).
Components: Consistency/Bootstrap and Decommission.

STUB: multi-node reproduction not yet encoded as a single-cluster Problem — see steps below.

Reproduction summary (2-node ring, RF=1 SimpleStrategy; concurrent decommission + cleanup):
  On a two-node ring (cass-0 + cass-1, RF=1 SimpleStrategy keyspace), cass-1 is decommissioned
  (`nodetool decommission --force`). Its ranges stream into the SURVIVING node cass-0 and become
  PENDING ranges on cass-0. While that throttled stream is still in flight (`nodetool netstats`
  shows cass-0 "Receiving N files"), `nodetool cleanup` is run on cass-0. On the buggy 4.1.0 build
  there is NO pending-range guard, so cleanup proceeds (EXIT=0) and SILENTLY DELETES the just-
  streamed data: cass-0's row COUNT drops from 7000 to 3375 and the loss is durable. The fixed
  4.1.1 build adds a guard in StorageService.forceKeyspaceCleanup() that throws
  "Node is involved in cluster membership changes. Not safe to run cleanup." and deletes nothing.

WHY THIS IS A STUB (do not flatten into one CQL string):
  The bug is a CONCURRENCY + multi-node ring defect: it needs a SECOND node to be decommissioning
  (so its ranges stream into the survivor and register as PENDING ranges) AT THE SAME TIME cleanup
  runs on the survivor, and the destructive window must be widened with stream throttling
  (`nodetool setstreamthroughput 1`) and ~9 MB/node of incompressible data so cleanup lands while
  netstats still shows "Receiving". A single deployed cluster has one merged dataset and no peer
  decommission, so a single `reproducer` CQL string CANNOT create the pending-range state the bug
  requires — flattening it would compile and register but silently NEVER reproduce the bug. The
  full multi-node steps from the evidence log are preserved verbatim in the `reproducer` field
  below so this can be promoted to a real multi-node Problem later. `continuous_reproducer` is left
  False (no working single-cluster looping reproducer pod) and `expected_output` is deliberately
  NOT set (the buggy COUNT 3375 is a run-specific, throttle-timing artifact, not a stable value to
  grep for; see field note below). See the authoritative evidence log:
  .claude/repro-evidence/repro-CASSANDRA-16418.md

Verbatim buggy signature (cassandra:4.1.0, cleanup loop on cass-0 while cass-1 decommissions):
  ===== iter 5 =====
  [netstats]   Receiving 28 files, 9122082 bytes total. Already received 2 files (7.14%), 28055 bytes total (0.31%)
  [cleanup]    EXIT=0           <-- cleanup SUCCEEDS with NO guard while node is receiving streamed data
  [count]      3375             <-- SILENT DATA LOSS: 7000 -> 3375
  >>>>>> DATA LOSS: count=3375 (was 7000) at iter 5 <<<<<<
  ... after `kubectl scale statefulset/cass --replicas=1` (so cass-1 cannot re-stream):
  SELECT COUNT(*) FROM repro16418.t;  =>  3375  (durable; ~3625 rows permanently lost)

Contrast on the FIXED 4.1.1 build, identical workload + topology (guard fires the instant cass-1
is Leaving/pending ranges exist on cass-0):
  [cleanup]
  error: Node is involved in cluster membership changes. Not safe to run cleanup.
  -- StackTrace --
  java.lang.RuntimeException: Node is involved in cluster membership changes. Not safe to run cleanup.
      at org.apache.cassandra.service.StorageService.forceKeyspaceCleanup(StorageService.java:3810)
  command terminated with exit code 2
"""

from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoCassandra16418(GenericCustomBuildProblem):
    # STUB: see module docstring. Multi-node (2-node ring, RF=1) decommission+cleanup concurrency
    # bug that only manifests when a peer is decommissioning (its ranges streaming into the
    # survivor as PENDING ranges) at the same time cleanup runs on the survivor. It CANNOT be
    # reproduced by a single-cluster CQL reproducer (the standard deploy gives one cluster with no
    # peer decommission and no pending ranges). Fields below are set so the problem registers and
    # carries the root-cause + full multi-node steps, but the `reproducer` is NOT a runnable
    # single-cluster CQL block (no continuous_reproducer, no expected_output — deliberately, to
    # avoid a false "it works" mitigation oracle that would loop something against the single
    # cluster, where the pending-range state does not exist, and report the bug reproduced).
    db_name = "cassandra"
    db_version = "4.1.0"
    source_git_ref = "cassandra-4.1.0"
    # 4.1.0 already ships the bug (buggy = fix patch 4.1.1 - 1), so deploy the stock image
    # instead of running a ~30-min `ant jar` source build.
    prebuilt_from_stock = True

    root_cause_file = "src/java/org/apache/cassandra/service/StorageService.java"
    root_cause_description = (
        "`nodetool cleanup` is unsafe to run on a node while it is involved in cluster membership "
        "changes (bootstrap or decommission). When a peer is decommissioning, its token ranges "
        "stream into the surviving node and register as PENDING ranges on it. On buggy 4.1.0, "
        "StorageService.forceKeyspaceCleanup() (line 3810) has NO check for pending ranges, so "
        "cleanup on the surviving receiver treats the just-streamed data as redundant (out of the "
        "node's owned ranges) and SILENTLY DELETES it — the streamed data vanishes, the "
        "decommission does not fail, and the cluster's data after the decommission is very "
        "different from the state before (observed COUNT drop 7000 -> 3375, durable). The fix "
        "(4.1.1) adds a guard: `if (tokenMetadata.getPendingRanges(keyspaceName, "
        "getBroadcastAddressAndPort()).size() > 0) throw new RuntimeException(\"Node is involved "
        "in cluster membership changes. Not safe to run cleanup.\");`, which rejects cleanup "
        "(exit 2) the instant pending ranges exist so no data is deleted. (4.1.0 still has an "
        "isJoined() guard in CompactionManager.performCleanup() that no-ops cleanup on a JOINING "
        "node, which is why the bootstrap symmetry is unverified and the reproduced scenario runs "
        "cleanup on the already-joined surviving receiver during a DECOMMISSION.)"
    )

    # Full multi-node reproduction steps (from the evidence log). This is NOT a runnable
    # single-cluster CQL block: it requires a 2-node ring, a backgrounded `nodetool decommission
    # --force` of the peer, stream throttling, incompressible data to widen the destructive
    # window, and a `nodetool cleanup` loop on the survivor timed to land WHILE the survivor is
    # receiving streamed files (pending ranges live). Encoded here verbatim so a future multi-node
    # harness can execute it; the single-cluster GenericCustomBuildProblem injector cannot run it.
    reproducer = """
-- STUB: 2-node ring (RF=1 SimpleStrategy), cassandra:4.1.0. Requires a peer decommission streaming
-- ranges into the survivor (PENDING ranges) CONCURRENT with `nodetool cleanup` on the survivor;
-- cannot be flattened into a single-cluster CQL reproducer (a single cluster has no peer
-- decommission and no pending ranges, which is exactly the state the bug requires).
-- ORDERING / TIMING IS LOAD-BEARING: cleanup must land WHILE netstats shows "Receiving".

-- ============================================================================
-- CLUSTER PRECONDITIONS (namespace repro-16418)
--   * 2-node StatefulSet `cass` (cass-0 = surviving receiver, cass-1 = node to decommission),
--     image cassandra:4.1.0.
--   * Both nodes UN (up/normal) before reproduction. Ring ownership: cass-0 51.2%, cass-1 48.8%.
--   * Stream throttle set LOW on BOTH nodes to widen the destructive window:
--       nodetool setstreamthroughput 1        (1 Mb/s)
-- ============================================================================

-- SCHEMA + DATA (run once via any coordinator while both nodes are UN). RF=1 is REQUIRED: with
-- RF>1 a redundant replica would mask the deleted data. Payloads must be INCOMPRESSIBLE random
-- bytes (hex of os.urandom) so ~9 MB/node does not compress to ~0 under LZ4 — repeated-char
-- payloads gave only ~2 s of streaming (too short); random payloads give ~70 s.
CREATE KEYSPACE repro16418 WITH replication = {'class':'SimpleStrategy','replication_factor':1};
CREATE TABLE repro16418.t (id int PRIMARY KEY, payload text);
-- Load ~7000 rows of incompressible random text (~9 MB/node total), e.g. for id in 0..6999:
--   INSERT INTO repro16418.t (id, payload) VALUES (<id>, '<hex(os.urandom(~650))>');
-- Then flush BOTH nodes so the data is on-disk (cass-0 ~8.47 MiB, cass-1 ~8.79 MiB):
--   (on cass-0)  nodetool flush repro16418
--   (on cass-1)  nodetool flush repro16418

-- BASELINE (run on cass-0):
CONSISTENCY ONE;
SELECT COUNT(*) FROM repro16418.t;          -- => 7000 (pre-decommission truth)

-- ============================================================================
-- STEP A — start the peer decommission IN THE BACKGROUND (run on cass-1)
-- ============================================================================
--   nodetool decommission --force
--   `--force` is REQUIRED: a plain decommission at N=2 is blocked by system_distributed RF=3
--   ("Not enough live nodes to maintain replication factor in keyspace system_distributed
--   (RF = 3, N = 2). Perform a forceful decommission to ignore."). This is expected Cassandra
--   behavior, not the bug. cass-1's ranges now stream into cass-0 and become PENDING on cass-0.

-- ============================================================================
-- STEP B — loop `nodetool cleanup` on the SURVIVOR cass-0 until it lands during streaming
-- ============================================================================
--   While the throttled stream is in flight, repeatedly (every few seconds) on cass-0:
--     nodetool netstats                      -- watch for: "Receiving N files, ... bytes total"
--     nodetool cleanup                        -- the destructive call (target it while Receiving)
--     cqlsh -e "CONSISTENCY ONE; SELECT COUNT(*) FROM repro16418.t;"
--   On 4.1.0, the iteration where netstats shows "Receiving" yields:
--     [netstats] Receiving 28 files, 9122082 bytes total. Already received 2 files (7.14%) ...
--     [cleanup]  EXIT=0          <-- NO pending-range guard; cleanup deletes the streamed data
--     [count]    3375            <-- SILENT DATA LOSS: 7000 -> 3375
--   (On FIXED 4.1.1 the same cleanup throws, exit 2:
--     "error: Node is involved in cluster membership changes. Not safe to run cleanup."
--     java.lang.RuntimeException ... at StorageService.forceKeyspaceCleanup(StorageService.java:3810))

-- ============================================================================
-- STEP C — make the loss DURABLE (so cass-1 cannot rejoin and re-stream the data back)
-- ============================================================================
--   The StatefulSet wants N replicas and will recreate/re-bootstrap the decommissioned pod, which
--   would re-stream data and MASK the loss. Pin replicas down immediately after the destructive
--   cleanup:
--     kubectl scale statefulset/cass -n repro-16418 --replicas=1
--   cass-0 is then the only node and owns 100%:
--     nodetool status repro16418   => UN ... 8.49 MiB ... 100.0%   (single node)

-- BUGGY SIGNATURE — the money query (run on cass-0 after the scale-down):
CONSISTENCY ONE;
SELECT COUNT(*) FROM repro16418.t;
--   Correct (fixed 4.1.1, cleanup rejected, nothing deleted):  7000
--   Buggy   (4.1.0, cleanup deleted streamed data):            3375   <-- ~3625 rows PERMANENTLY lost
"""

    # Deliberately NOT set (stub): continuous_reproducer stays False and expected_output stays
    # None. This IS a wrong-result bug in nature (COUNT 7000 -> 3375), but the
    # "wrong-result => set expected_output" rule only applies to RUNNABLE single-cluster
    # reproducers. The reproducer above needs a real 2-node ring with a concurrent peer
    # decommission + stream throttling + precisely-timed cleanup; looping a SELECT against the
    # single cluster the standard deploy provides would NOT reproduce the 3375 result (the single
    # cluster holds one intact dataset with no pending ranges), so arming the
    # ReproducerPodMitigationOracle via expected_output would falsely report the bug
    # "reproduced/mitigated". Moreover 3375 is a non-deterministic, throttle-timing artifact (it
    # depends on which streamed files had landed when cleanup ran), not a stable value to grep for.
    # This stub contributes only the diagnosis oracle (root cause) until a multi-node harness
    # exists. (Mirrors auto_cassandra_16671 / auto_cassandra_16577, the same multi-node-ring shape.)
    continuous_reproducer = False
