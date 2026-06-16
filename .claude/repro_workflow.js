export const meta = {
  name: 'cassandra-bug-repro',
  description: 'Reproduce the 9 remaining deployable Cassandra bugs from bugs.txt in the kind cluster (record only, no fixes)',
  phases: [
    { title: 'Preflight', detail: 'one agent verifies kubectl/cqlsh access; gates the fan-out' },
    { title: 'Reproduce', detail: 'one agent per target bug, parallel, deploy stock buggy pods/rings in kind' },
    { title: 'Trunk-only', detail: 'one agent re-confirms the 32 trunk-only bugs have no deployable released image' },
  ],
}

// ---------------------------------------------------------------------------
// Shared briefing. Every reproduction agent gets this, then a per-bug section.
// ---------------------------------------------------------------------------
const BRIEFING = `You are reproducing ONE Apache Cassandra bug as part of the SREGym db-bug-reproduction effort.
Follow the methodology of the sregym-db-bug-reproduction skill (manual / hand-crafted mode).

## Hard constraints (read carefully)
- Use the EXISTING local kind cluster (kubectl context kind-kind, 4 nodes). Reproduce by deploying
  Cassandra PODS into kind. DO NOT use 'docker run' / docker directly to reproduce. Multi-node rings
  inside kind are allowed and encouraged where the bug needs them.
- DO NOT edit, patch, or "fix" ANY file in the repo, the SREGym tooling, or Cassandra. This session is
  RECORD-ONLY. If you hit a SREGym tooling bug, write it into the 'tooling_findings' field; do not fix it.
- ISOLATION: create your OWN namespace named "repro-<NUM>" (e.g. repro-21245) and use a unique keyspace.
  Do NOT touch, mutate, or tear down any pre-existing namespace (cass-*, repro-smoke, k8ssandra-operator,
  cert-manager) — other agents and prior reproductions depend on them. Tear down ONLY namespaces you
  create, and only AFTER you have captured all evidence into your log file.

## Methodology (skill)
- The buggy version is the released fix patch minus 1, and the official 'cassandra:<buggy>' Docker image
  already contains the bug, so a single stock pod (or ring) reproduces it with NO source build.
- Released image ceilings (highest tags that exist on Docker Hub): 3.11->19, 4.0->20, 4.1->11, 5.0->8.
  A fixed-version A/B control is only possible when the fix patch is <= the ceiling for its series.
- Drive the reproducer via: kubectl exec -n <ns> <pod> -- cqlsh -e "<CQL>"  and  kubectl exec ... -- nodetool ...

## Single-node pod template (substitute <NS>, <VER>)
apiVersion: v1
kind: Pod
metadata: { name: cass, namespace: <NS>, labels: { app: cass } }
spec:
  terminationGracePeriodSeconds: 5
  containers:
  - name: cass
    image: cassandra:<VER>
    env:
    - { name: MAX_HEAP_SIZE, value: "1024M" }
    - { name: HEAP_NEWSIZE, value: "256M" }
    - { name: CASSANDRA_DC, value: "dc1" }
    - { name: CASSANDRA_ENDPOINT_SNITCH, value: "GossipingPropertyFileSnitch" }
    resources: { requests: { cpu: "500m", memory: "1536Mi" }, limits: { memory: "2560Mi" } }
Wait for readiness with: kubectl wait -n <NS> --for=condition=Ready pod/cass --timeout=300s
then poll until CQL answers: kubectl exec -n <NS> cass -- cqlsh -e "SELECT now() FROM system.local"

## Multi-node ring template (substitute <NS>, <VER>, <N> = replica count, e.g. 2 or 3)
apiVersion: v1
kind: Service
metadata: { name: cass, namespace: <NS> }
spec: { clusterIP: None, selector: { app: cass }, ports: [ { port: 9042, name: cql }, { port: 7000, name: gossip } ] }
---
apiVersion: apps/v1
kind: StatefulSet
metadata: { name: cass, namespace: <NS> }
spec:
  serviceName: cass
  replicas: <N>
  podManagementPolicy: OrderedReady   # pod-0 (seed) becomes Ready before pod-1 starts
  selector: { matchLabels: { app: cass } }
  template:
    metadata: { labels: { app: cass } }
    spec:
      terminationGracePeriodSeconds: 10
      containers:
      - name: cass
        image: cassandra:<VER>
        env:
        - { name: MAX_HEAP_SIZE, value: "1024M" }
        - { name: HEAP_NEWSIZE, value: "256M" }
        - { name: CASSANDRA_SEEDS, value: "cass-0.cass.<NS>.svc.cluster.local" }
        - { name: CASSANDRA_CLUSTER_NAME, value: "repro" }
        - { name: CASSANDRA_DC, value: "dc1" }
        - { name: CASSANDRA_ENDPOINT_SNITCH, value: "GossipingPropertyFileSnitch" }
        readinessProbe:
          exec: { command: ["/bin/sh","-c","nodetool statusgossip | grep -q running && cqlsh -e 'select now() from system.local'"] }
          initialDelaySeconds: 90
          periodSeconds: 15
          timeoutSeconds: 10
          failureThreshold: 40
        resources: { requests: { cpu: "500m", memory: "1536Mi" }, limits: { memory: "2560Mi" } }
After deploy, wait for all <N> pods Ready (kubectl rollout status statefulset/cass -n <NS> --timeout=900s),
then confirm the ring with: kubectl exec -n <NS> cass-0 -- nodetool status  (expect <N> lines starting 'UN').
Multi-node first boot is slow (each node bootstraps sequentially); be patient, poll nodetool status.

## Procedure
1. FIRST read your primary source: /tmp/jira_issues/<BUG>.json (use: cat or python -c json). The 'fields.description'
   holds the real reproducer. State the exact reproducer you extracted BEFORE deploying. Trust this JSON over
   your own memory of Cassandra internals.
2. Deploy the needed topology at the buggy version. Run the reproducer. Capture the VERBATIM buggy output.
3. If a fixed image exists (fix patch <= ceiling), run the IDENTICAL workload on the fixed version as an A/B
   control and capture that it does NOT misbehave.
4. Write a detailed evidence log (every key command + its raw output, especially the buggy signature and the
   control) to /tmp/repro-<BUG>.md.
5. Tear down namespaces you created (kubectl delete ns repro-<NUM> --wait=false).

## Evidence bar (this determines your disposition)
- disposition = "reproduced" REQUIRES a verbatim buggy signature: the exact exception class + stack frame, the
  exact server error message, or the concrete wrong query result. Put that single most-telling line in
  'verbatim_signature'. No verbatim signature => you may NOT claim "reproduced".
- disposition = "confirmed-blocked": the bug needs infrastructure that cannot be staged here (in-JVM multi-node
  dtest internals, a precise timing/partition window, a crash injected between two syscalls, full mTLS PKI you
  cannot stand up in budget, or the JSON contains NO concrete reproducer). State the SPECIFIC mechanism that
  cannot be staged. This is a CLEAN, acceptable outcome — do not thrash trying to force it.
- disposition = "not-reproducible": you reached the buggy code path but it behaves correctly (e.g. shadowed by
  client validation), with evidence.
- disposition = "inconclusive": ran out of budget mid-attempt; say exactly where you stopped and the next step.

## Budget
Bounded: roughly 15-25 minutes of wall-clock effort, or about 3 deploy-and-test cycles. Prefer a precise
"confirmed-blocked with the specific un-stageable mechanism" over an open-ended struggle.
`

// ---------------------------------------------------------------------------
// Per-bug target data. priorVerdict / plan come from repro-progress.md Phase 3.
// fixedImage = a fixed cassandra:<tag> that EXISTS (<= ceiling) for an A/B control, else null.
// ---------------------------------------------------------------------------
const BUGS = [
  {
    id: 'CASSANDRA-21245', num: '21245', buggy: '5.0.8', fixedImage: null, topology: 'single',
    category: 'storage-engine',
    priorVerdict: 'blocked-risk, but root cause is FULLY understood and this is the HIGHEST-VALUE target. A dedicated pod "cass-21245" (stock 5.0.8 with max_space_usable_for_compactions_in_percentage: 0.0002 appended to cassandra.yaml) is ALREADY RUNNING in namespace cass-21245 — you MAY reuse it (it is dedicated to this bug, not shared). On the prior attempt the first `nodetool compact` returned rc=0 with NO "Not enough space" log, i.e. the lever did not trip.',
    plan: `Root cause: CompactionTask.buildCompactionCandidatesForAvailableDiskSpace compares cfs.getExpectedCompactedFileSize(...) (reported UNCOMPRESSED for a compressed table, pre-fix) against Directories.getAvailableSpaceForCompactions = (usableSpace - min_free_space_per_drive) * max_space_usable_for_compactions_in_percentage. Because the expected size is uncompressed, compaction is wrongly denied even though the compressed data fits.
GOAL: make the available-space lever small enough that the UNCOMPRESSED expected size exceeds it, so a 'nodetool compact' of a highly-compressible table fails with "Not enough space" and logs an "expected write size" equal to the UNCOMPRESSED total (proving the uncompressed-size accounting bug).
Steps: build a table with DeflateCompressor (or LZ4) holding very compressible data (e.g. rows of 'z'*1MiB) so on-disk size is tiny but uncompressed is large; STCS with autocompaction OFF; flush >=2 sstables. Then SHRINK available space: the prior lever (percentage 0.0002) wasn't enough — recompute. Options to drive available-space below the uncompressed total: lower the percentage further, and/or raise min_free_space_per_drive_in_mb in cassandra.yaml so (usableSpace - min_free) * pct < uncompressed_total. You may need a fresh pod with a tuned cassandra.yaml appended in the pod command before 'docker-entrypoint.sh cassandra -f'. Capture the server log line showing the denial and the expected (uncompressed) write size. NOTE: fix is 5.0.9 (no image) => control is within-version reasoning (the same compact succeeds once the lever is relaxed / on incompressible data the accounting matches).`,
  },
  {
    id: 'CASSANDRA-20871', num: '20871', buggy: '4.0.19', fixedImage: '4.0.20', topology: 'single',
    category: 'cql-semantics(counter)',
    priorVerdict: 'blocked-hard. Counter + repaired-data AIOOBE. Single-node feasible if you can mark sstables repaired OFFLINE.',
    plan: `Root cause: with repaired_data_tracking_for_range_reads_enabled=true (and/or repaired_data_tracking_for_partition_reads_enabled=true) in cassandra.yaml, a range/partition read over counter cells that live in REPAIRED sstables hits an ArrayIndexOutOfBoundsException in CounterContext.headerLength (empty/short counter context).
Steps: set the repaired_data_tracking flag(s) true in cassandra.yaml (append before start, then restart/redeploy). CREATE a counter table, UPDATE counters, nodetool flush. Mark the sstable(s) REPAIRED offline: stop the node or use the offline tool 'sstablerepairedset --really-set --is-repaired <sstable>' (path under /var/lib/cassandra/data/<ks>/<tbl>-*/), then restart. Then issue a range read (SELECT * FROM ks.tbl;) or partition read that scans the repaired counter sstable. Capture the verbatim AIOOBE + CounterContext.headerLength frame from the server log / read failure. Control: identical steps on cassandra:4.0.20 (fix) read cleanly.`,
  },
  {
    id: 'CASSANDRA-21219', num: '21219', buggy: '5.0.6', fixedImage: '5.0.7', topology: 'single',
    category: 'cql-semantics(security)',
    priorVerdict: 'blocked-hard. CVE-2026-27314 privilege escalation; needs full mTLS PKI (MutualTlsAuthenticator + client cert truststore/keystore + roles) before ADD IDENTITY authz can be tested.',
    plan: `Root cause: a non-superuser with permission can 'ADD IDENTITY <cert-identity> TO ROLE <role>' binding a client certificate identity to a SUPERUSER role, escalating privileges, because the authorization check on ADD IDENTITY is missing/insufficient.
Steps (hard): configure MutualTlsAuthenticator (client_encryption_options optional=false, require_client_auth=true, authenticator: MutualTlsAuthenticator or MutualTlsWithPasswordFallbackAuthenticator), generate a CA + server keystore/truststore + a client cert, mount them, restart. Create a normal role with limited grants, connect via the client cert, attempt ADD IDENTITY '<superuser-cert-identity>' TO ROLE cassandra (superuser). Buggy 5.0.6: the bind SUCCEEDS (escalation). Control 5.0.7: it is REJECTED (Unauthorized). If standing up the full mTLS PKI exceeds budget, confirm-blocked with the specific PKI step that could not be staged, and cite the exact ADD IDENTITY authz code path.`,
  },
  {
    id: 'CASSANDRA-20976', num: '20976', buggy: '5.0.5', fixedImage: '5.0.6', topology: 'single',
    category: 'storage-engine',
    priorVerdict: 'blocked-hard: prior session found the Jira body is ONLY a mailing-list link with NO concrete reproducer. Your job: read the JSON, and IF it truly has no runnable reproducer, confirm-blocked. If it DOES contain enough to reconstruct (BTI sstable + token-range query AssertionError), attempt it.',
    plan: `Reported: a BTI-format sstable triggers an AssertionError on a token-range query. To attempt: CREATE TABLE ... WITH ... and set the sstable format to BTI (5.0 'bti' format; may need sstable_format/trie index settings), write data, flush, then run a token-range SELECT (e.g. SELECT ... WHERE token(pk) > X AND token(pk) <= Y). Capture any AssertionError verbatim. If the JSON gives no concrete schema/query, confirm-blocked (no concrete reproducer).`,
  },
  {
    id: 'CASSANDRA-21290', num: '21290', buggy: '4.1.11', fixedImage: null, topology: 'single',
    category: 'other-db-behavior',
    priorVerdict: 'blocked-hard, likely NON-DETERMINISTIC. The bug is an empty heartbeat file produced if the process crashes between create() and write(); the fix makes the heartbeat-file write atomic. This is crash-window hardening.',
    plan: `Root cause: the gossip generation heartbeat file (/var/lib/cassandra/data/.../<no> or saved generation file) is created then written non-atomically; a crash in that window leaves an EMPTY file, and on restart the node fails to read its generation. The fix writes to a temp file and atomically renames.
Attempt: this requires injecting a crash precisely between the create and write syscalls — not deterministically reproducible from kubectl. You may demonstrate the CONSEQUENCE: stop the pod, truncate the heartbeat/generation file to 0 bytes (simulating the crash artifact), restart, and capture the startup error reading the empty file. If you can show that 4.1.11 fails to start / errors on an empty generation file while 4.1.x-with-fix tolerates it, note it — but the fix has no released image (4.1.12 does not exist), so the control is within-version. Most likely outcome: confirm-blocked (crash-window, non-deterministic), citing the exact file and the atomic-rename fix.`,
  },
  {
    id: 'CASSANDRA-21332', num: '21332', buggy: '5.0.8', fixedImage: null, topology: 'multi',
    category: 'cql-semantics(SAI/RFP)',
    priorVerdict: 'blocked-hard, likely IN-JVM-DTEST-ONLY. Static-SAI + range-tombstone data resurrection via Replica Filtering Protection. Now attemptable with the 4-node cluster — try a real 2-3 node ring, but it may be unreproducible outside an in-JVM dtest that forces per-replica divergent data.',
    plan: `Root cause: with a static column indexed by SAI, read_repair=NONE, and per-node DIVERGENT data (one replica holds a range tombstone, another holds a stale row), a SELECT using the SAI index can RESURRECT data the tombstone should hide, during the Replica Filtering Protection (RFP) path.
Attempt on a 2-node (RF=2) ring: CREATE TABLE with a static column, CREATE CUSTOM INDEX ... USING 'StorageAttachedIndex' on it, WITH read_repair='NONE'. Then create divergence: write a row, flush; on node A apply a range tombstone, on node B keep the stale row (use nodetool to stop gossip / write at CL=ONE to a specific node, or stop one node while writing). Then SELECT via the SAI predicate and check whether the deleted/old value is resurrected. Capture the wrong (resurrected) row as the verbatim signature. If forcing per-replica divergence is not achievable with kubectl/nodetool (it normally requires in-JVM message interception), confirm-blocked citing the in-JVM-dtest requirement. Fix is 5.0.9 (no image) => within-version reasoning.`,
  },
  {
    id: 'CASSANDRA-20877', num: '20877', buggy: '4.0.19', fixedImage: '4.0.20', topology: 'multi',
    category: 'distributed-multinode',
    priorVerdict: 'blocked-hard, now attemptable. FINALIZED incremental-repair sessions in system.repairs are never cleaned after range movement (bootstrap/decommission), because isSuperseded stays false.',
    plan: `Root cause: after a node bootstraps/decommissions (range movement), FINALIZED incremental repair sessions in the system.repairs table are not cleaned up; LocalSessions considers them not superseded (isSuperseded=false), so they accumulate forever.
Attempt on a 2-node ring (RF=2): CREATE a keyspace/table, write data, run 'nodetool repair' (incremental, default) so a repair session FINALIZES and rows appear in system.repairs (SELECT * FROM system.repairs;). Then trigger RANGE MOVEMENT: bootstrap a 3rd node (scale the StatefulSet to 3 / add a node) or decommission one. After the movement + the cleanup interval, re-query system.repairs and show the FINALIZED session is STILL present (not cleaned). Verbatim signature: the persisted system.repairs row(s) with state=FINALIZED that survive the range movement. Control: cassandra:4.0.20 (fix) cleans them. NOTE: this needs incremental repair to actually finalize; verify the repair finalized before asserting.`,
  },
  {
    id: 'CASSANDRA-21132', num: '21132', buggy: '5.0.6', fixedImage: '5.0.7', topology: 'multi',
    category: 'distributed-multinode',
    priorVerdict: 'blocked-hard, now attemptable. SAI index-status gossip encoding AssertionError at startup on a multi-node cluster with many SAI indexes (a gossip-encoding feature-gate race).',
    plan: `Root cause: the index-status gossip state encoding hits an AssertionError when there are many SAI indexes (the encoded index-status map exceeds a size/format assumption), tripping during gossip on a homogeneous multi-node cluster at startup.
Attempt on a 2-3 node ring: create MANY keyspaces/tables each with one or more SAI indexes (CREATE CUSTOM INDEX ... USING 'StorageAttachedIndex'), e.g. dozens-to-hundreds of indexes, to bloat the index-status gossip payload. Then restart a node (or roll the StatefulSet) and watch the system log for the AssertionError in the index-status / gossip encoding path. Capture the verbatim AssertionError + frame. Control: identical index load on cassandra:5.0.7 (fix) starts clean. The exact index COUNT to trip it may need tuning — increase until it fires or budget is hit.`,
  },
  {
    id: 'CASSANDRA-21428', num: '21428', buggy: '4.0.20', fixedImage: null, topology: 'multi',
    category: 'distributed-multinode',
    priorVerdict: 'blocked-hard, likely TIMING-SENSITIVE. A node stays DOWN after an ECHO_REQ times out during a transient partition, because a stale inflightEcho entry is never cleared. Needs a precisely-timed partition.',
    plan: `Root cause: when an ECHO_REQ (echo request used to confirm a peer is UP) times out due to a transient network partition, a stale entry in the inflightEcho map is left behind, so the node never re-marks the peer UP — it is stuck DOWN even after connectivity returns.
Attempt on a 2-node ring: once both are UN, induce a TRANSIENT partition between them (e.g. block port 7000 with an iptables rule inside one pod for ~10-20s spanning an echo timeout, then unblock). After connectivity returns, check 'nodetool status' / 'nodetool gossipinfo' on each node: buggy 4.0.20 may show the peer stuck DN despite restored connectivity. Verbatim signature: nodetool status showing 'DN' for a reachable peer + log evidence of the timed-out echo / stale inflightEcho. This is timing-sensitive; if you cannot reliably hit the echo-timeout window with kubectl-level partitioning, confirm-blocked citing the precise-timing requirement. Fix is 4.0.21 (no image) => within-version reasoning.`,
  },
]

function buildPrompt(b) {
  return BRIEFING +
    `\n\n=========================================================\n` +
    `## YOUR BUG: ${b.id}  (buggy version ${b.buggy}, ${b.category}, ${b.topology}-node)\n` +
    `Primary source JSON: /tmp/jira_issues/${b.id}.json\n` +
    `Fixed-version control image: ${b.fixedImage ? 'cassandra:' + b.fixedImage + ' (exists — run the A/B control)' : 'NONE exists (fix patch > released ceiling) — use within-version reasoning for the control'}\n` +
    `Your namespace: repro-${b.num}   (the multi-node ring or single pod goes here)\n\n` +
    `### Prior assessment (do not re-derive from scratch)\n${b.priorVerdict}\n\n` +
    `### Reproduction plan\n${b.plan}\n\n` +
    `Write your detailed evidence log to /tmp/repro-${b.id}.md. Then return the structured result. ` +
    `Remember: RECORD ONLY, never edit repo/tooling/Cassandra files; "confirmed-blocked with the specific ` +
    `un-stageable mechanism" is a clean outcome; do not claim "reproduced" without a verbatim signature.`
}

const REPRO_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['bug', 'buggy_version', 'disposition', 'topology', 'trigger', 'verbatim_signature', 'evidence', 'control', 'notes', 'tooling_findings', 'log_path', 'namespaces_created', 'torn_down'],
  properties: {
    bug: { type: 'string' },
    buggy_version: { type: 'string' },
    disposition: { type: 'string', enum: ['reproduced', 'confirmed-blocked', 'not-reproducible', 'inconclusive'] },
    topology: { type: 'string', description: 'e.g. "single-node" or "2-node ring"' },
    trigger: { type: 'string', description: 'one-line trigger' },
    verbatim_signature: { type: 'string', description: 'exact buggy output line(s): exception class + frame, server error, or wrong result. Empty unless reproduced.' },
    evidence: { type: 'string', description: 'key commands run and their raw outputs / what was observed' },
    control: { type: 'string', description: 'fixed-version A/B result, or why no control image exists' },
    notes: { type: 'string' },
    tooling_findings: { type: 'string', description: 'any SREGym tooling bug encountered (record-only); "none" if none' },
    log_path: { type: 'string' },
    namespaces_created: { type: 'array', items: { type: 'string' } },
    torn_down: { type: 'boolean' },
  },
}

const PREFLIGHT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['ok', 'kubectl_works', 'can_create_ns', 'cqlsh_works', 'details'],
  properties: {
    ok: { type: 'boolean', description: 'true only if kubectl works AND a throwaway ns could be created+deleted AND cqlsh answered on an existing pod' },
    kubectl_works: { type: 'boolean' },
    can_create_ns: { type: 'boolean' },
    cqlsh_works: { type: 'boolean' },
    details: { type: 'string' },
  },
}

const TRUNK_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['confirmed_no_image', 'checked', 'findings'],
  properties: {
    confirmed_no_image: { type: 'boolean' },
    checked: { type: 'array', items: { type: 'string' }, description: 'a few example trunk-only bug numbers / fix versions checked' },
    findings: { type: 'string' },
  },
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------
phase('Preflight')
const pf = await agent(
  `Preflight check for a Cassandra bug-reproduction fan-out on a kind cluster. Verify, using Bash:
1. kubectl works: run 'kubectl config current-context' (expect kind-kind) and 'kubectl get ns'.
2. You can create AND delete a namespace: 'kubectl create ns repro-preflight-check' then 'kubectl delete ns repro-preflight-check --wait=false'.
3. cqlsh works against an existing pod WITHOUT mutating it (read-only): 'kubectl exec -n cass-5-0-6 cass -- cqlsh -e "SELECT now() FROM system.local"' (if that ns/pod is gone, try cass-5-0-5 or repro-smoke).
Set ok=true ONLY if all three succeed. Put the exact commands+outputs (or errors) in details. Do not deploy anything else.`,
  { label: 'preflight', phase: 'Preflight', schema: PREFLIGHT_SCHEMA, agentType: 'general-purpose' }
)

if (!pf || !pf.ok) {
  log('PREFLIGHT FAILED — aborting fan-out. ' + (pf ? pf.details : 'agent returned null'))
  return { aborted: true, reason: 'preflight failed', preflight: pf }
}
log('Preflight OK: kubectl=' + pf.kubectl_works + ' createNs=' + pf.can_create_ns + ' cqlsh=' + pf.cqlsh_works)

phase('Reproduce')
log('Fanning out ' + BUGS.length + ' reproduction agents (' + BUGS.filter(b => b.topology === 'multi').length + ' multi-node, ' + BUGS.filter(b => b.topology === 'single').length + ' single-node)')
const results = await parallel(
  BUGS.map(b => () => agent(buildPrompt(b), { label: 'repro:' + b.num, phase: 'Reproduce', schema: REPRO_SCHEMA, agentType: 'general-purpose' }))
)

phase('Trunk-only')
const trunk = await agent(
  `Re-confirm the deployability ceiling for the 32 "trunk-only" Cassandra bugs in bugs.txt. These were fixed
ONLY in 6.0-alpha*/6.0/7.x with NO released X.Y.Z patch, so the stock-image fast path cannot reproduce them,
and a source build is out of scope (this session must NOT use docker to reproduce).
Verify the conclusion still holds by spot-checking that no released image exists above the ceilings
(3.11->19, 4.0->20, 4.1->11, 5.0->8). For example check whether tags cassandra:6.0, cassandra:5.0.9,
cassandra:4.0.21 exist on Docker Hub (use: curl -s 'https://hub.docker.com/v2/repositories/library/cassandra/tags?page_size=100' | grep -oE '"name":"[^"]+"' | head -60, or kubectl/crane if available; if no network, reason from the known ceilings).
Do NOT build anything. Return confirmed_no_image=true if the ceilings hold, list a few examples checked, and
summarize in findings. RECORD ONLY.`,
  { label: 'trunk-confirm', phase: 'Trunk-only', schema: TRUNK_SCHEMA, agentType: 'general-purpose' }
)

return {
  preflight: pf,
  results: results.filter(Boolean),
  null_results: results.filter(r => !r).length,
  trunk,
}
