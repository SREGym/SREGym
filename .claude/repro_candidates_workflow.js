export const meta = {
  name: 'cassandra-candidate-repro',
  description: 'Reproduce bugs.txt candidates in kind (parameterized by args.mode; resumable; record-only)',
  phases: [
    { title: 'Preflight', detail: 'gate: verify kubectl/cqlsh' },
    { title: 'Load', detail: 'filter candidates by mode/tier, skip already-done' },
    { title: 'Reproduce', detail: 'one agent per candidate; deploy stock pod/ring in kind, run reproducer' },
  ],
}

const BRIEFING = `You are reproducing ONE Apache Cassandra bug CANDIDATE for SREGym, following the
sregym-db-bug-reproduction skill (manual/hand-crafted mode). The candidate came from an automated
text-triage, so its tags (topology, confidence, trigger) are HINTS — the Jira body is the ground truth.

## Hard constraints
- Use the EXISTING kind cluster (context kind-kind, 4 nodes). Reproduce with Cassandra PODS in kind.
  DO NOT use docker directly. Multi-node rings inside kind are allowed.
- RECORD-ONLY: never edit/patch/fix any repo file, SREGym tooling, or Cassandra. Put any SREGym tooling
  issue you notice in 'tooling_findings'; do not fix it.
- ISOLATION: create your OWN namespace 'repro-<NUM>' (NUM = the issue number) and a unique keyspace. Do
  NOT touch any pre-existing namespace (cass-*, repro-smoke, k8ssandra-operator, cert-manager).
- TEARDOWN IS MANDATORY: after you have written your evidence log, delete EVERY namespace you created
  ('kubectl delete ns <ns> --wait=false'). Leaving pods running breaks the rest of the run. Set
  torn_down=true only if you actually deleted them. This is non-negotiable at this scale.

## Primary source first
Read /tmp/jira_repro/<KEY>.json (fields: summary, description, fixVersions, components). The description
is the real reproducer/symptom. The classifier's one-line trigger is only a hint — if the body shows a
different reproducer, topology, or that the bug is NOT observable / NOT reproducible, FOLLOW THE BODY and
record the correction in 'tag_correction'. (Prior session example: a bug tagged "in-jvm-dtest-only"
actually reproduced on a real ring via gossip isolation — do not let a tag anchor a false "blocked".)

## Buggy image + control
The buggy version is given (its public image cassandra:<buggy> exists). A FIXED-version A/B control is
possible only if (buggy patch + 1) <= the released ceiling for its line: 3.11->19, 4.0->20, 4.1->11,
5.0->8. If so, run the IDENTICAL workload on cassandra:<buggy-patch+1> and show it does NOT misbehave.
If no fixed image exists, use within-version reasoning for the control.

## Single-node pod template (substitute <NS>,<VER>)
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
Wait: kubectl wait -n <NS> --for=condition=Ready pod/cass --timeout=300s ; then poll
'kubectl exec -n <NS> cass -- cqlsh -e "SELECT now() FROM system.local"' until it answers.
Config-gated bugs: append a cassandra.yaml block in the pod command before 'docker-entrypoint.sh cassandra -f'.

## Multi-node ring template (substitute <NS>,<VER>,<N>=2 or 3)
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
  podManagementPolicy: OrderedReady
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
After deploy: 'kubectl rollout status statefulset/cass -n <NS> --timeout=900s', then 'nodetool status'
should show <N> 'UN'. For per-replica divergence without an in-JVM dtest: 'nodetool disablegossip' on the
peers, confirm they show DN, write at CONSISTENCY ONE, 'nodetool flush', re-enable gossip; verify physical
divergence with 'sstabledump' on each node's local Data.db.

## Procedure
1. Read /tmp/jira_repro/<KEY>.json; state the exact reproducer you extracted (from body, or derive it).
2. Deploy the buggy version (single pod, or ring if the bug needs one — use your judgment over the tag).
3. Run the reproducer; capture the VERBATIM buggy output (exact exception+frame, error message, or wrong
   query result). Run the A/B control if a fixed image exists.
4. Write a detailed evidence log (commands + raw outputs, especially the buggy signature + control) to
   /tmp/repro-<KEY>.md.
5. TEAR DOWN every namespace you created.

## Disposition + evidence bar
- "reproduced": REQUIRES a verbatim buggy signature. Put the single most-telling line in 'verbatim_signature'
  as a LITERAL COPY of the cqlsh/server output (the exact exception+frame, error text, or wrong result row) —
  NOT a paraphrase or summary. The identical text MUST also appear in your /tmp log. No verbatim signature
  => you may NOT claim reproduced.
- "not-observable": you reached the code path but there is no client/operator-visible change (internal
  refinement/hardening).
- "not-reproducible": the body's mechanism does not fire on the buggy image (shadowed by validation, a
  disabled feature, etc.) — with evidence.
- "confirmed-blocked": needs infrastructure not stageable here (in-JVM message interception, precise
  timing/partition, crash-between-syscalls, or no concrete reproducer) — name the specific mechanism.
- "needs-fix-test": the only reproducer is the fix's unit/dtest and you could not fetch/adapt it in budget.
- "blocked-disk-constrained": the reproducer INHERENTLY needs more data than the disk budget (multi-GiB
  sstables, heavy stress writes, a >2GiB index, large vector sets). Do NOT fill the disk — record this
  disposition and name the data requirement. The host root disk is small (~63 GiB) and shared by the cluster.
- "inconclusive": ran out of budget mid-attempt; say where you stopped.

## DATA SIZE (critical on this host)
Use the SMALLEST dataset that triggers the bug — a few rows, not bulk data. Do NOT run cassandra-stress or
write multi-GiB data unless the bug's mechanism truly requires volume. For rings, the StatefulSet template
uses EPHEMERAL storage (no PVC) by default; only add a small (<=1Gi) volumeClaimTemplate if the bug needs
data to survive a restart. If you cannot trigger the bug without exceeding ~1-2 GiB, use
"blocked-disk-constrained" rather than filling the disk (which breaks co-running agents).

## Budget
Bounded: ~12-20 min wall-clock or ~3 deploy-and-test cycles. A precise "confirmed-blocked / not-observable
with the specific reason" is a clean outcome — do not thrash. Always tear down before returning.`

const APPENDIX_NOTE = `\n\n## APPENDIX MODE (linked-test-only)
This candidate's reproducer is NOT in the Jira body — it lives in the fix's unit/dtest. Make ONE attempt
to fetch it: search the apache/cassandra GitHub for the fix commit/test (e.g. curl the GitHub code-search
or raw test file referenced in the issue), adapt its schema+operations into a cqlsh/nodetool reproducer,
and run it. If you cannot fetch/adapt it within budget, record disposition "needs-fix-test" with what you
found. Still tear down anything you deployed.`

function buildPrompt(c, mode) {
  return BRIEFING + (mode === 'appendix' ? APPENDIX_NOTE : '') +
    '\n\n=========================================================\n' +
    '## YOUR CANDIDATE: ' + c.key + '\n' +
    'Buggy version: ' + c.buggy + '   (fixed-control image candidate: cassandra:<buggy patch+1> if <= ceiling)\n' +
    'Classifier HINT (verify against the body, correct if wrong): topology=' + (c.topo || (mode === 'ring' ? 'ring' : '1node')) + ', confidence=' + (c.conf || '?') + '\n' +
    'Hint trigger: ' + (c.trigger || '(none provided — derive the reproducer from the Jira body)') + '\n' +
    'Primary source: /tmp/jira_repro/' + c.key + '.json   |   Your namespace: repro-' + c.key.replace('CASSANDRA-', '') + '\n' +
    'Write your evidence log to /tmp/repro-' + c.key + '.md, then return the structured result. RECORD ONLY; tear down.'
}

const REPRO_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['key', 'buggy_version', 'disposition', 'topology', 'trigger', 'verbatim_signature', 'evidence', 'control', 'notes', 'tag_correction', 'tooling_findings', 'log_path', 'namespaces_created', 'torn_down'],
  properties: {
    key: { type: 'string' },
    buggy_version: { type: 'string' },
    disposition: { type: 'string', enum: ['reproduced', 'not-observable', 'not-reproducible', 'confirmed-blocked', 'needs-fix-test', 'blocked-disk-constrained', 'inconclusive'] },
    topology: { type: 'string' },
    trigger: { type: 'string' },
    verbatim_signature: { type: 'string' },
    evidence: { type: 'string' },
    control: { type: 'string' },
    notes: { type: 'string' },
    tag_correction: { type: 'string', description: '"none" or describe how reality differed from the classifier hint' },
    tooling_findings: { type: 'string' },
    log_path: { type: 'string' },
    namespaces_created: { type: 'array', items: { type: 'string' } },
    torn_down: { type: 'boolean' },
  },
}

function chunk(a, n) { const o = []; for (let i = 0; i < a.length; i += n) o.push(a.slice(i, i + n)); return o }

// The batch (mode + candidates) is provided via the file /tmp/repro_batch.json, written by the caller.
// (args delivery proved unreliable; a file + a trivial reader agent is deterministic.)

phase('Preflight')
const pf = await agent(
  'Preflight for a Cassandra reproduction fan-out. Verify with Bash: (1) kubectl config current-context = kind-kind and kubectl get ns works; (2) you can create+delete a namespace (kubectl create ns repro-preflight-x && kubectl delete ns repro-preflight-x --wait=false); (3) /tmp/jira_repro/ exists with CASSANDRA-*.json files (ls); (4) df -h / shows at least ~5G free. Return ok=true if kubectl AND createns AND the json dir checks pass. (The fan-out agents deploy their OWN fresh Cassandra pods and verify cqlsh themselves, so a pre-existing cqlsh target is NOT required — do not gate on it.) Report the free-disk figure in details.',
  { label: 'preflight', phase: 'Preflight', schema: { type: 'object', additionalProperties: false, required: ['ok', 'details'], properties: { ok: { type: 'boolean' }, details: { type: 'string' } } }, agentType: 'general-purpose' }
)
if (!pf || !pf.ok) { log('PREFLIGHT FAILED: ' + (pf ? pf.details : 'null')); return { aborted: true, preflight: pf } }
log('Preflight OK')

phase('Load')
const loaded = await agent(
  'Run this exact python3 and return its JSON output. It reads the batch file and drops already-done candidates:\n' +
  'python3 - <<PY\n' +
  'import json, glob, os\n' +
  "b = json.load(open('/tmp/repro_batch.json'))\n" +
  "done = {os.path.basename(p)[len('repro-'):-3] for p in glob.glob('/tmp/repro-CASSANDRA-*.md')}\n" +
  "cands = [c for c in b['candidates'] if c['key'] not in done]\n" +
  "print(json.dumps({'mode': b.get('mode','single'), 'ringConcurrency': b.get('ringConcurrency',6), 'candidates': cands}))\n" +
  'PY\n' +
  'Return EXACTLY the JSON object the script printed (the full candidates array, do not drop or add any).',
  { label: 'load', phase: 'Load', schema: { type: 'object', additionalProperties: false, required: ['mode', 'ringConcurrency', 'candidates'], properties: { mode: { type: 'string' }, ringConcurrency: { type: 'number' }, candidates: { type: 'array', items: { type: 'object', additionalProperties: true } } } }, agentType: 'general-purpose' }
)
const MODE = (loaded && loaded.mode) || 'single'
const RINGC = (loaded && loaded.ringConcurrency) || 6
const CANDS = (loaded && Array.isArray(loaded.candidates)) ? loaded.candidates : []
log('Loaded ' + CANDS.length + ' candidates to reproduce (mode=' + MODE + ', ringConcurrency=' + RINGC + ')')
if (CANDS.length === 0) { return { done: true, reproduced: 0, results: [], note: 'no candidates in /tmp/repro_batch.json (or all done)' } }

phase('Reproduce')
// Worker-pool: caps concurrent agents at RINGC (disk/CPU safety) with NO chunk barrier, so a slow agent
// occupies only its own slot and never blocks the others. RINGC tuned per-run via the batch file.
let done = 0
async function runPool(items, conc) {
  const out = new Array(items.length)
  let next = 0
  async function worker() {
    while (true) {
      const i = next++
      if (i >= items.length) return
      const c = items[i]
      out[i] = await agent(buildPrompt(c, MODE), { label: 'repro:' + c.key.replace('CASSANDRA-', ''), phase: 'Reproduce', schema: REPRO_SCHEMA, agentType: 'general-purpose' })
      done++
      if (done % 5 === 0 || done === items.length) log('progress ' + done + '/' + items.length + ' (reproduced: ' + out.filter(x => x && x.disposition === 'reproduced').length + ')')
    }
  }
  await Promise.all(Array.from({ length: Math.min(conc, items.length || 1) }, () => worker()))
  return out
}
const results = await runPool(CANDS, RINGC)
const ok = results.filter(Boolean)
return {
  mode: MODE, attempted: CANDS.length,
  results: ok, null_results: results.length - ok.length,
  reproduced: ok.filter(r => r.disposition === 'reproduced').length,
}
