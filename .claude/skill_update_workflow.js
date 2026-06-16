export const meta = {
  name: 'skill-update-repro-to-problem',
  description: 'Update the sregym-db-bug-reproduction SKILL.md with a "reproduced bug -> benchmark Problem" section',
  phases: [
    { title: 'Update', detail: 'one agent adds the new section to SKILL.md' },
    { title: 'Review', detail: 'one agent reviews the edit for accuracy against the codebase' },
  ],
}

const SPEC = [
  'Add a new top-level section to .claude/skills/sregym-db-bug-reproduction/SKILL.md titled',
  '"## From a reproduced bug to a benchmark Problem". Place it AFTER the "## Hand-crafting a problem"',
  'subsection (or, if that is nested, after the "## Phase 2 — Runtime" section) and BEFORE "## Adding a new database".',
  'Do NOT delete or rewrite existing content; only insert the new section. Keep the skill\'s terse, factual style.',
  '',
  'FIRST read these files so the section is accurate: the current SKILL.md; the templates',
  'sregym/conductor/problems/auto_cassandra_20050.py (GenericCustomBuildProblem, auto-discovered),',
  'sregym/conductor/problems/cassandra_20108.py and cassandra_18105.py (CassandraBugProblem, manual registry,',
  'custom inject_fault), and the base sregym/conductor/problems/generic_custom_build.py (note the NEW',
  '`prebuilt_from_stock` per-problem override field that was just added).',
  '',
  'The new section MUST cover, accurately and concisely:',
  '',
  '1. PURPOSE. Every reproduced DB bug should become a runnable benchmark Problem. The authoritative source',
  '   for each bug is its reproduction evidence log at `.claude/repro-evidence/repro-CASSANDRA-<n>.md` (and the',
  '   machine-readable `.claude/repro-evidence/candidate_results.json`): use it for the buggy version, the EXACT',
  '   reproducer steps, the verbatim buggy signature, and the A/B control. Trust the log over memory.',
  '',
  '2. TWO BASE CLASSES (state when to use each, with the oracle difference):',
  '   - GenericCustomBuildProblem -> file `auto_<db>_<number>.py` (e.g. auto_cassandra_20050.py). AUTO-DISCOVERED',
  '     by ProblemRegistry._load_auto_generated() (no registry edit needed). Gives BOTH a diagnosis oracle',
  '     (LLMAsAJudgeOracle on the root cause) AND a mitigation oracle (ReproducerPodMitigationOracle on a',
  '     looping reproducer pod) when continuous_reproducer=True. For a bug that already ships in the released',
  '     image (buggy version = fix patch - 1), set `prebuilt_from_stock = True` so it deploys the STOCK buggy',
  '     image instead of running a ~30-min source build (ant jar). This is the PREFERRED pattern for the',
  '     stock-reproducible Cassandra bugs in `bugs.txt`.',
  '   - CassandraBugProblem -> file `cassandra_<number>.py` (e.g. cassandra_20108.py, cassandra_18105.py).',
  '     Deploys a stock cluster via the K8ssandra operator and runs `trigger_cql`; DIAGNOSIS-ONLY',
  '     (mitigation_oracle = None). Requires a MANUAL registry edit (import + an entry in PROBLEM_REGISTRY in',
  '     sregym/conductor/problems/registry.py). Use when you want a hand-written custom inject_fault and do not',
  '     need the mitigation oracle.',
  '',
  '3. REPRODUCTION-SHAPE DECISION TREE (read the evidence log, then pick the encoding):',
  '   - Single-node, pure CQL (CREATE/INSERT/DELETE/SELECT that triggers the bug): GenericCustomBuildProblem',
  '     with `reproducer` = the CQL block and `continuous_reproducer = True`. (auto_cassandra_20050 style.)',
  '   - WRONG-RESULT bug (returns/persists an incorrect value rather than an error): ALSO set `expected_output`',
  '     to the buggy value so the mitigation oracle probe greps for it (Ready = bug present, NotReady = fixed).',
  '   - CONFIG-GATED (needs a cassandra.yaml block such as startup_checks/guardrails, or a pre-staged file):',
  '     use `_setup_preconditions_sql` or override `setup_preconditions()`; for startup-failure bugs set',
  '     `crash_on_startup = True` (inject runs preconditions, swaps the buggy image, waits for CrashLoop).',
  '   - NODETOOL / FLUSH SEQUENCE (e.g. disableautocompaction + flush x N + garbagecollect): override',
  '     inject_fault() to run the nodetool steps via kubectl exec (see cassandra_20108.py for the kubectl-exec',
  '     + background-loop pattern), then the CQL.',
  '   - MULTI-NODE RING or CROSS-VERSION (per-replica divergence, scale/bootstrap, repair, sstableloader between',
  '     versions): these need multi-pod orchestration that a single `reproducer` CQL string CANNOT express.',
  '     Write a CLEARLY-MARKED STUB — set db_version/source_git_ref/root_cause_* and put the full multi-node',
  '     steps from the evidence log in a `reproducer`/docstring TODO — rather than flattening a multi-node',
  '     reproduction into one CQL (which would compile and register but silently NOT reproduce the bug).',
  '',
  '4. REQUIRED FIELDS for the GenericCustomBuildProblem pattern: db_name="cassandra"; db_version=<buggy>',
  '   (= released fix patch - 1); source_git_ref="cassandra-<buggy>"; root_cause_file (the buggy source file);',
  '   root_cause_description (1-3 sentences); reproducer (the CQL/steps); continuous_reproducer=True;',
  '   prebuilt_from_stock=True (for stock-image bugs); expected_output (only for wrong-result bugs).',
  '',
  '5. ORACLE SEMANTICS: diagnosis = LLMAsAJudgeOracle(expected=root_cause). mitigation =',
  '   ReproducerPodMitigationOracle with expect_unready = (expected_output is not None): for wrong-result bugs',
  '   Ready=bug-present/NotReady=fixed; for error/crash bugs NotReady=bug-present/Ready=fixed.',
  '',
  '6. REGISTRATION: auto_*.py whose class subclasses GenericCustomBuildProblem are auto-discovered (the loader',
  '   checks issubclass(GenericCustomBuildProblem)). cassandra_*.py / CassandraBugProblem need a manual',
  '   import + PROBLEM_REGISTRY entry in registry.py. Problem id = the file stem for auto_*.py.',
  '',
  '7. VERIFICATION (CRITICAL — state this prominently): verify a generated Problem STATICALLY only:',
  '   `uv run python -m py_compile <file>` and confirm ProblemRegistry() loads it (class registration; the',
  '   loader stores the class, it does NOT call __init__). NEVER instantiate or deploy a Problem to "verify"',
  '   it — instantiation triggers the image build / operator deploy and is slow and disk-heavy. Separately,',
  '   spot-check that the encoded `reproducer` matches the buggy path in the evidence log (not the A/B control).',
  '',
  'After inserting the section, run `uv run python -m py_compile` is not needed (markdown), but DO re-read the',
  'edited region to confirm the section is well-formed and placed correctly. Return a short summary of what you added.',
].join('\n')

phase('Update')
const upd = await agent(
  SPEC,
  { label: 'skill-update', phase: 'Update', schema: { type: 'object', additionalProperties: false, required: ['done', 'summary', 'section_heading', 'lines_added'], properties: { done: { type: 'boolean' }, summary: { type: 'string' }, section_heading: { type: 'string' }, lines_added: { type: 'number' } } }, agentType: 'general-purpose' }
)
log('Skill update: ' + (upd ? upd.summary : 'null'))

phase('Review')
const rev = await agent(
  'Review the new section "## From a reproduced bug to a benchmark Problem" just added to ' +
  '.claude/skills/sregym-db-bug-reproduction/SKILL.md. Read that section AND cross-check every concrete claim ' +
  'against the codebase: (a) GenericCustomBuildProblem now has a `prebuilt_from_stock` field ' +
  '(grep sregym/conductor/problems/generic_custom_build.py); (b) auto_*.py auto-discovery requires a ' +
  'GenericCustomBuildProblem subclass (grep registry.py _load_auto_generated); (c) the oracle classes named ' +
  '(LLMAsAJudgeOracle, ReproducerPodMitigationOracle) and expect_unready semantics match generic_custom_build.py; ' +
  '(d) the file-naming + registration claims are correct; (e) the decision-tree shapes are consistent with ' +
  'cassandra_20108.py (custom inject_fault) and auto_cassandra_20050.py (simple reproducer). Fix any inaccuracy ' +
  'directly in SKILL.md (small edits only; do not remove the section). Report any corrections made and whether ' +
  'the section is accurate and well-placed.',
  { label: 'skill-review', phase: 'Review', schema: { type: 'object', additionalProperties: false, required: ['accurate', 'corrections', 'notes'], properties: { accurate: { type: 'boolean' }, corrections: { type: 'string' }, notes: { type: 'string' } } }, agentType: 'general-purpose' }
)
log('Skill review: accurate=' + (rev ? rev.accurate : 'null'))
return { update: upd, review: rev }
