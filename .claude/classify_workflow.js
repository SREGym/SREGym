export const meta = {
  name: 'cassandra-bug-classify',
  description: 'Classify 433 deployable Cassandra fixed-bug candidates for likely-reproducibility (text-only triage)',
  phases: [
    { title: 'Classify', detail: 'one agent per ~18-issue batch; read self-contained JSON, classify per rubric' },
  ],
}

// Rubric is a plain string (single quotes for inline code — NO backticks, to keep template literals safe).
const RUBRIC = [
  'You triage Apache Cassandra fixed-bug reports for whether they look REPRODUCIBLE as an SREGym problem.',
  'This is TEXT-ONLY judgment (you do not run anything). For each assigned issue key, read the self-contained',
  'file /tmp/jira_new/<KEY>.json (fields: key, summary, description [may be truncated], fixVersions,',
  'components, buggy, fix). The buggy version is already computed in the "buggy" field (= lowest deployable',
  'released fix patch minus 1; its public image exists). Classify each issue with these fields:',
  '',
  'db_behavior (bool): true if this is a real client- or operator-visible DATABASE defect. FALSE for',
  '  CI/test-infra, test-only logic, build/packaging/docs/dependencies, and internal-tooling-OUTPUT bugs',
  "  (nodetool/cqlsh cosmetic output, JMX/CCM/cassandra-stress internals, virtual-table internal refactors).",
  '  NOTE: a nodetool command that TRIGGERS real DB behavior (compaction, repair, garbagecollect, guardrails)',
  '  IS db_behavior — judge the effect, not the entry point.',
  '',
  'observable_symptom (string): NAME the concrete client- or operator-visible effect — a wrong query result,',
  '  an exception/error returned to the client or logged, a stuck/incorrect cluster state, data loss or',
  '  corruption, or a crash / failed startup. If the fix is an INTERNAL refinement with NO externally-visible',
  "  change in normal use (an error-TYPE swap, validation hardening that rejects only already-invalid input,",
  '  a pure refactor, a log-message tweak), set this to "none". An issue with observable_symptom="none" is',
  '  NOT wanted even if it has repro steps (cf. CASSANDRA-20917 error-type refinement, 21389 snapshot-name',
  '  hardening). Quote the phrase from the body that establishes the symptom in your reason.',
  '',
  'repro_class (enum):',
  '  single-node          = reproducible on ONE pod via cqlsh / nodetool / sstable tools.',
  '  multi-node-stageable = needs a ring, but stageable in kind with nodetool + scaling + GOSSIP ISOLATION',
  '                         (nodetool disablegossip on peers + write at CONSISTENCY ONE + flush gives',
  '                         per-replica divergence). Most "needs N nodes / repair / bootstrap / SAI gossip"',
  '                         bugs are stageable.',
  '  in-jvm-dtest-only    = needs in-PROCESS message interception (IMessageFilters verb-drop) or',
  '                         executeInternal node-local writes WITH NO nodetool/gossip-isolation analog.',
  '                         Use this ONLY when there is genuinely no external analog. Do NOT mark a bug',
  '                         dtest-only merely because the fix\'s test happens to be a dtest (a real ring +',
  '                         gossip isolation reproduced CASSANDRA-21332, which looked dtest-only).',
  '  timing-partition     = needs a precise timing / network-partition window (e.g. echo-timeout race) that',
  '                         kubectl-level partitioning cannot hit deterministically.',
  '  crash-window         = needs a crash injected between two operations (non-deterministic).',
  '  none                 = no reproducible mechanism described.',
  '',
  'reproducer_provenance (enum):',
  '  in-body                            = a runnable reproducer (CQL / nodetool / explicit steps) is IN the description.',
  '  derivable-from-described-mechanism = no verbatim script, but the body gives enough (schema + operation +',
  '                                       condition) to CONSTRUCT one.',
  '  linked-test-only                   = the only reproducer is the fix\'s unit/dtest (referenced, not in the',
  '                                       body). Still a candidate, but reproduction needs fetching that test.',
  '  absent                             = no reproducer and not derivable (body is a bare stack trace, a',
  '                                       mailing-list link, or prose only — cf. CASSANDRA-20976).',
  '',
  'category (enum): cql-semantics | storage-engine | distributed-multinode | other-db-behavior | not-db-behavior',
  'buggy_version (string): copy the "buggy" field from the JSON.',
  'one_line_trigger (string): a concise trigger, in the style "DELETE range tombstone + higher-ts row + SELECT DISTINCT -> error".',
  'confidence (enum): high = clearly db_behavior + a named observable symptom + in-body/derivable reproducer +',
  '  clearly single-node or multi-node-stageable. medium = plausible but with some uncertainty. low = guessing.',
  'reason (string): 1-3 sentences; QUOTE the phrase in the body that establishes the symptom and the reproducer',
  '  location. This is used for spot-check verification, so be specific and do not invent details not in the file.',
  '',
  'Return one verdict object per assigned key (all of them, even not-db-behavior ones — mark them accordingly).',
].join('\n')

function chunk(a, n) { const o = []; for (let i = 0; i < a.length; i += n) o.push(a.slice(i, i + n)); return o }

function buildPrompt(batch) {
  return RUBRIC +
    '\n\n=== YOUR BATCH (' + batch.length + ' issues) ===\n' +
    'Read each file then classify it. Keys:\n' + batch.join('\n') +
    '\n\nReturn {"results": [ <one verdict per key above> ]}.'
}

const VERDICT = {
  type: 'object', additionalProperties: false,
  required: ['key', 'db_behavior', 'observable_symptom', 'repro_class', 'reproducer_provenance', 'category', 'buggy_version', 'one_line_trigger', 'confidence', 'reason'],
  properties: {
    key: { type: 'string' },
    db_behavior: { type: 'boolean' },
    observable_symptom: { type: 'string' },
    repro_class: { type: 'string', enum: ['single-node', 'multi-node-stageable', 'in-jvm-dtest-only', 'timing-partition', 'crash-window', 'none'] },
    reproducer_provenance: { type: 'string', enum: ['in-body', 'derivable-from-described-mechanism', 'linked-test-only', 'absent'] },
    category: { type: 'string', enum: ['cql-semantics', 'storage-engine', 'distributed-multinode', 'other-db-behavior', 'not-db-behavior'] },
    buggy_version: { type: 'string' },
    one_line_trigger: { type: 'string' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    reason: { type: 'string' },
  },
}
const BATCH_SCHEMA = { type: 'object', additionalProperties: false, required: ['results'], properties: { results: { type: 'array', items: VERDICT } } }

phase('Enumerate')
const enumResult = await agent(
  'Run this exact command and return its output as the keys array: ' +
  'ls /tmp/jira_new/CASSANDRA-*.json | xargs -n1 basename | sed "s/\\.json$//" | sort -u . ' +
  'Return {"keys": [<every CASSANDRA-NNNNN key, one per file>]}. There should be a few hundred.',
  { label: 'enumerate', phase: 'Enumerate', schema: { type: 'object', additionalProperties: false, required: ['keys'], properties: { keys: { type: 'array', items: { type: 'string' } } } }, agentType: 'general-purpose' }
)
const KEYS = (enumResult && Array.isArray(enumResult.keys)) ? enumResult.keys : (Array.isArray(args) ? args : [])
if (KEYS.length === 0) { log('No candidate keys found — aborting'); return { aborted: true, reason: 'no keys' } }
const BATCHES = chunk(KEYS, 18)
log('Classifying ' + KEYS.length + ' candidates in ' + BATCHES.length + ' batches of <=18')

phase('Classify')
const results = await parallel(
  BATCHES.map((batch, i) => () => agent(buildPrompt(batch), { label: 'classify:' + (i + 1), phase: 'Classify', schema: BATCH_SCHEMA, agentType: 'general-purpose' }))
)

const verdicts = results.filter(Boolean).flatMap(r => (r.results || []))
log('Collected ' + verdicts.length + ' verdicts from ' + results.filter(Boolean).length + '/' + BATCHES.length + ' batches')
return { verdicts, batches_ok: results.filter(Boolean).length, batches_total: BATCHES.length, null_batches: results.filter(r => !r).length }
