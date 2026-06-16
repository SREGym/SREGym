export const meta = {
  name: 'cassandra-problem-gen',
  description: 'Implement a benchmark Problem (auto_cassandra_<n>.py) for each reproduced Cassandra bug, using the skill + evidence logs',
  phases: [
    { title: 'Preflight', detail: 'verify skill section, evidence logs, py_compile' },
    { title: 'Load', detail: 'read the problem set' },
    { title: 'Generate', detail: 'one agent per bug: classify shape, write Problem, py_compile' },
  ],
}

const BRIEF = [
  'You implement ONE SREGym benchmark Problem for a Cassandra bug that was already REPRODUCED, following the',
  'sregym-db-bug-reproduction skill. This is CODE GENERATION only — you write a .py file and py_compile it.',
  'Do NOT deploy anything, do NOT instantiate the Problem class, do NOT touch the kind cluster.',
  '',
  'READ THESE FIRST (in order):',
  '  1. The skill section "## From a reproduced bug to a benchmark Problem" in',
  '     .claude/skills/sregym-db-bug-reproduction/SKILL.md — it has the decision tree and the field list. FOLLOW IT.',
  '  2. Your bug\'s reproduction evidence log: .claude/repro-evidence/repro-<KEY>.md — this is AUTHORITATIVE for the',
  '     exact buggy reproducer steps, the buggy version, the root-cause file/description, and the verbatim signature.',
  '     Trust this log over your own memory of Cassandra. Use the BUGGY reproducer, NOT the A/B control workload.',
  '  3. The template sregym/conductor/problems/auto_cassandra_20050.py (the simple GenericCustomBuildProblem shape).',
  '     For non-trivial shapes also skim sregym/conductor/problems/cassandra_20108.py (custom inject_fault pattern).',
  '',
  'CLASSIFY the reproduction shape from the log, then encode it (per the skill decision tree):',
  '  - SINGLE-NODE pure CQL -> reproducer = the CQL block; continuous_reproducer = True.',
  '  - WRONG-RESULT (query returns/persists an incorrect value, no exception) -> ALSO set expected_output to the',
  '    BUGGY value (the wrong value the buggy build returns/persists — NOT the correct value).',
  '  - CONFIG-GATED (needs a cassandra.yaml block, or a pre-staged file) -> set _setup_preconditions_sql or override',
  '    setup_preconditions(); set crash_on_startup = True for startup-failure bugs.',
  '  - NODETOOL / FLUSH SEQUENCE -> override inject_fault() (see cassandra_20108.py kubectl-exec pattern) to run the',
  '    nodetool steps then the CQL.',
  '  - MULTI-NODE RING or CROSS-VERSION (per-replica divergence, scale/bootstrap/repair, sstableloader across versions)',
  '    -> write a CLEARLY-MARKED STUB: set db_name/db_version/source_git_ref/root_cause_* and put the FULL multi-node',
  '    steps from the log into the `reproducer` string AND a module docstring "STUB: multi-node reproduction not yet',
  '    encoded as a single-cluster Problem — see steps below." Do NOT flatten a multi-node repro into one CQL.',
  '',
  'WRITE the file sregym/conductor/problems/auto_cassandra_<NUM>.py with:',
  '  - A module docstring: bug title, JIRA URL (https://issues.apache.org/jira/browse/CASSANDRA-<NUM>), buggy->fixed',
  '    versions, a 2-4 line reproduction summary, and the verbatim buggy signature from the log.',
  '  - from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem',
  '  - class AutoCassandra<NUM>(GenericCustomBuildProblem): with class attributes:',
  '      db_name = "cassandra"; db_version = "<BUGGY>"; source_git_ref = "cassandra-<BUGGY>"; prebuilt_from_stock = True;',
  '      root_cause_file = "<buggy source file path from the log, e.g. src/java/org/apache/cassandra/...>";',
  '      root_cause_description = "<1-3 sentences from the log>"; reproducer = "<the buggy CQL/steps>";',
  '      continuous_reproducer = True; and expected_output only for wrong-result bugs.',
  '  - Use a triple-quoted Python string for the reproducer. Keep CQL statements semicolon-terminated.',
  '',
  'VERIFY STATICALLY (required): run  uv run python -m py_compile sregym/conductor/problems/auto_cassandra_<NUM>.py',
  'and fix any syntax error. Do NOT import the registry, instantiate the class, or run the Problem (that triggers a',
  'build/deploy). The file is auto-discovered by ProblemRegistry because it is named auto_*.py and subclasses',
  'GenericCustomBuildProblem.',
  '',
  'Return the structured result describing what you wrote.',
].join('\n')

function buildPrompt(c) {
  const num = c.num
  return BRIEF +
    '\n\n=========================================================\n' +
    '## YOUR BUG: CASSANDRA-' + num + '   (buggy version ' + (c.buggy || '(read from the log)') + ', topology hint: ' + (c.topology || 'unknown') + ')\n' +
    'Evidence log (authoritative): .claude/repro-evidence/repro-CASSANDRA-' + num + '.md\n' +
    'One-line trigger hint: ' + (c.trigger || '(see log)') + '\n' +
    'Output file: sregym/conductor/problems/auto_cassandra_' + num + '.py   |   class AutoCassandra' + num + '\n' +
    'If the buggy version in the log differs from the hint, TRUST THE LOG.'
}

const GEN_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['key', 'file_path', 'shape', 'is_stub', 'buggy_version', 'expected_output_set', 'compiles', 'reproducer_source', 'notes'],
  properties: {
    key: { type: 'string' },
    file_path: { type: 'string' },
    shape: { type: 'string', enum: ['single-node-cql', 'wrong-result', 'config-gated', 'nodetool-sequence', 'multi-node-stub', 'cross-version-stub', 'other'] },
    is_stub: { type: 'boolean' },
    buggy_version: { type: 'string' },
    expected_output_set: { type: 'boolean' },
    compiles: { type: 'boolean' },
    reproducer_source: { type: 'string', enum: ['log', 'derived-from-log', 'stub-todo'] },
    notes: { type: 'string' },
  },
}

phase('Preflight')
const pf = await agent(
  'Preflight for a code-generation fan-out (NO cluster). Verify with Bash: (1) the skill section exists: ' +
  'grep -c "From a reproduced bug to a benchmark Problem" .claude/skills/sregym-db-bug-reproduction/SKILL.md (expect 1); ' +
  '(2) evidence logs exist: ls .claude/repro-evidence/repro-CASSANDRA-*.md | wc -l (expect ~90); ' +
  '(3) the template exists: ls sregym/conductor/problems/auto_cassandra_20050.py; ' +
  '(4) py_compile works: uv run python -m py_compile sregym/conductor/problems/auto_cassandra_20050.py && echo OK; ' +
  '(5) /tmp/problem_set.json exists (ls). Return ok=true only if all pass.',
  { label: 'preflight', phase: 'Preflight', schema: { type: 'object', additionalProperties: false, required: ['ok', 'details'], properties: { ok: { type: 'boolean' }, details: { type: 'string' } } }, agentType: 'general-purpose' }
)
if (!pf || !pf.ok) { log('PREFLIGHT FAILED: ' + (pf ? pf.details : 'null')); return { aborted: true, preflight: pf } }
log('Preflight OK')

phase('Load')
const loaded = await agent(
  'Run this exact command and return its JSON as {"candidates": [...]}: ' +
  'cat /tmp/problem_set.json . It is a JSON array of {key,num,buggy,topology,trigger}. Also drop any whose output ' +
  'file already exists: for each, the file is sregym/conductor/problems/auto_cassandra_<num>.py — exclude entries ' +
  'where that file already exists (ls). Return the remaining array verbatim under "candidates".',
  { label: 'load', phase: 'Load', schema: { type: 'object', additionalProperties: false, required: ['candidates'], properties: { candidates: { type: 'array', items: { type: 'object', additionalProperties: true } } } }, agentType: 'general-purpose' }
)
const CANDS = (loaded && Array.isArray(loaded.candidates)) ? loaded.candidates : []
log('Loaded ' + CANDS.length + ' bugs to implement as Problems')
if (CANDS.length === 0) { return { done: true, generated: 0, results: [] } }

phase('Generate')
const results = await parallel(
  CANDS.map(c => () => agent(buildPrompt(c), { label: 'gen:' + c.num, phase: 'Generate', schema: GEN_SCHEMA, agentType: 'general-purpose' }))
)
const ok = results.filter(Boolean)
return {
  attempted: CANDS.length,
  results: ok,
  null_results: results.length - ok.length,
  generated: ok.filter(r => r.compiles).length,
  stubs: ok.filter(r => r.is_stub).length,
}
