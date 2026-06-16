"""Extract a bug reproducer (SQL / shell / script) from an issue body using an LLM.

Falls back to a lightweight regex scan if the LLM is unavailable or returns nothing.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


# ── LLM response parsing ──────────────────────────────────────────────────────

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _response_text(message) -> str:
    """Return the first text block's content from an Anthropic Messages response.

    Needed because extended thinking puts a ThinkingBlock at content[0], which
    has no `.text` attribute — the actual answer lives in a later block.
    """
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    return ""


def _extract_json(raw: str) -> dict | None:
    """Best-effort JSON parse of an LLM response.

    Handles: markdown fences around the object, prose before/after the object,
    and raw newlines inside string values (strict=False).
    """
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = s.strip()
    if not s:
        return None
    try:
        return json.loads(s, strict=False)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJECT_RE.search(s)
    if not m:
        return None
    try:
        return json.loads(m.group(0), strict=False)
    except json.JSONDecodeError:
        return None


# ── Regex fallback (used when LLM is unavailable) ────────────────────────────

# Matches markdown heading (## To Reproduce), Jira heading (h2. To Reproduce), and bold
# (**To Reproduce:**) formats.
_REPRO_SECTION_RE = re.compile(
    r"(?:^|\n)"
    r"(?:(?:#{1,4}\s*|h[1-6]\.\s*)|\*{1,2})?"
    r"(?:steps?\s+to\s+reproduc|to\s+reproduc|how\s+to\s+reproduc|reproduc)[^\n]*"
    r"(?:\*{1,2})?\s*:?\s*\n"
    r"(.*?)(?=\n(?:#{1,4}\s|h[1-6]\.\s|\*{1,2}\S)|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_JIRA_CODE_BLOCK_RE = re.compile(r"\{(code|noformat)(?::([^}]*))?\}\n?(.*?)\{\1\}", re.DOTALL | re.IGNORECASE)
_JIRA_PROMPT_RE = re.compile(
    r"^\s*(?:\w+@cqlsh(?::\w+)?>\s*|cqlsh(?::\w+)?>\s*|(?:mysql|postgres|=#|=>)\s*)",
    re.IGNORECASE,
)
_JIRA_OUTPUT_RE = re.compile(
    r"^\s*(?:InvalidRequest|Error(?:\s+from\s+server)?|Exception|Traceback|WARN|ERROR)\b|code=\d+|message=",
    re.IGNORECASE,
)
# Untagged blocks ("") are excluded: they're commonly used for error output, not executable code.
_EXEC_LANGS = {"sql", "mysql", "cql", "cqlsh", "bash", "sh", "shell", "python", "py", "go"}


def _jira_language_hint(hint: str | None) -> str:
    if not hint:
        return ""
    hint = hint.strip().lower()
    m = re.search(r"(?:^|[,\s;|])language\s*=\s*([\w+-]+)", hint)
    if m:
        return m.group(1)
    m = re.match(r"([\w+-]+)", hint)
    return m.group(1) if m else ""


def _clean_jira_code_block(content: str) -> str:
    lines = []
    for line in content.splitlines():
        cleaned = _JIRA_PROMPT_RE.sub("", line).strip()
        if not cleaned or _JIRA_OUTPUT_RE.search(cleaned):
            continue
        lines.append(cleaned)
    return "\n".join(lines).strip()


def _regex_extract(body: str) -> str | None:
    for section in _REPRO_SECTION_RE.finditer(body):
        blocks = [
            m.group(2).strip() for m in _CODE_BLOCK_RE.finditer(section.group(1)) if m.group(1).lower() in _EXEC_LANGS
        ]
        if blocks:
            return "\n\n".join(blocks)
    for m in _CODE_BLOCK_RE.finditer(body):
        if m.group(1).lower() in _EXEC_LANGS:
            return m.group(2).strip()
    blocks = []
    for m in _JIRA_CODE_BLOCK_RE.finditer(body):
        language = _jira_language_hint(m.group(2))
        cleaned = _clean_jira_code_block(m.group(3))
        if language in _EXEC_LANGS or (not language and _SQL_KEYWORDS.search(cleaned) and not _is_prose(cleaned)):
            blocks.append(cleaned)
    if blocks:
        return "\n\n".join(blocks)
    return None


# ── Prose detector ───────────────────────────────────────────────────────────

_SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|USE|SET|WITH|EXPLAIN|SHOW|CALL|LOAD)\b",
    re.IGNORECASE,
)
_PROSE_STEP_RE = re.compile(r"^\s*\d+\.", re.MULTILINE)


def _is_prose(text: str) -> bool:
    """Return True if text looks like numbered prose steps rather than executable code."""
    step_matches = len(_PROSE_STEP_RE.findall(text))
    sql_matches = len(_SQL_KEYWORDS.findall(text))
    # If there are more numbered steps than SQL keywords, it's prose.
    return step_matches >= 2 and step_matches > sql_matches


# ── Soundness guards for auto-extracted reproducers ───────────────────────────

# Issues filed by Sentry's bot contain crash telemetry (panic, stack trace) but
# no replayable reproducer — SQL column names are redacted to `_._` and the
# plan gist is an opaque base64 blob. There is no executable form to recover.
_SENTRY_MARKERS = (
    "this issue was auto filed by sentry",
    "auto filed by sentry",
)

# Phrases that almost never appear verbatim in legitimate SQL / mongosh / shell
# but show up constantly in Go/Rust/Java panic dumps. Presence of any is strong
# evidence the "reproducer" is actually a stack trace pasted into the wrong slot.
_PANIC_MARKERS = (
    "panic:",
    "runtime error:",
    "-- stack trace:",
    "assertion failure",
    "Wraps: (",
    "Error types:",
    "-- report composition:",
    "*safedetails.",
    "*withstack.",
    "*barriers.barrierErr",
    "goroutine ",
)

# Any one of these keywords is enough to consider a text block "potentially
# executable". Covers SQL DDL/DML, mongosh (db.x / use / show / ObjectId /
# ISODate / sh. / rs.), and common DB shell commands.
_EXEC_KEYWORD_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|USE|WITH|EXPLAIN|SHOW|"
    r"BEGIN|COMMIT|ROLLBACK|GRANT|REVOKE|LOAD|CALL|SET|TRUNCATE|VALUES|TABLE)\b"
    r"|db\.|ObjectId\(|ISODate\(|\bsh\.|\brs\.|\buse\s+\w"
    r"|\betcdctl\b|\bcurl\b|\bexport\b|\bkill\b|\betcdutl\b",
    re.IGNORECASE,
)


def _is_sentry_auto_filed(body: str) -> bool:
    if not body:
        return False
    low = body.lower()
    return any(marker in low for marker in _SENTRY_MARKERS)


def _sanity_check_reproducer(reproducer: str, buggy_output: str | None) -> tuple[bool, str]:
    """Return (ok, reason). If ok is False, the reproducer should be discarded.

    Guards against four common extraction failure modes:
      1. Circular — ``buggy_output`` pasted in as the reproducer, so any client
         that echoes input (``cockroach sql``, ``mongosh``) will match trivially.
      2. Panic dump — a stack trace extracted from an error-display code block.
      3. Redacted SQL — Sentry-filed issues use ``_._`` for every column name.
      4. No executable keyword — pure prose or an unrelated fragment.
    """
    if not reproducer:
        return False, "empty reproducer"

    if buggy_output:
        buggy_norm = re.sub(r"\s+", "", buggy_output).strip()
        repro_norm = re.sub(r"\s+", "", reproducer).strip()
        # The length floor keeps single-word outputs (e.g. "ERROR") from triggering.
        if len(buggy_norm) > 30 and buggy_norm in repro_norm:
            return False, (
                "reproducer contains buggy_output verbatim — likely the crash traceback was extracted as the reproducer"
            )

    for marker in _PANIC_MARKERS:
        if marker in reproducer:
            return False, (
                f"contains panic/traceback marker {marker!r} — "
                f"reproducer appears to be a crash dump, not an executable script"
            )

    if reproducer.count("_._") >= 3:
        return False, (
            "contains redacted SQL placeholders (_._) — issue is likely "
            "Sentry-auto-filed with column names stripped; no replayable query"
        )

    if not _EXEC_KEYWORD_RE.search(reproducer):
        return False, (
            "no SQL / mongosh / shell executable keywords detected — "
            "reproducer may be prose or a non-executable fragment"
        )

    return True, ""


# ── LLM extraction ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a database reliability engineer who reads bug reports and writes complete, runnable
reproduction scripts. Your job is not just to copy code from the issue — it is to REASON
about what the full end-to-end reproduction flow must look like and SYNTHESIZE any missing
steps using your knowledge of the database system.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL DISTINCTION — reproducer vs. buggy_output
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - The REPRODUCER is what you EXECUTE (SQL statements, shell commands).
  - The BUGGY_OUTPUT is what the database PRINTS when the bug fires (error messages,
    wrong query results, stack traces, validation failures).
  - These are NEVER the same thing. A block that IS an error/stack trace is buggy_output,
    not reproducer — even if it appears in a "To Reproduce" section.
  - Signals that a block is buggy_output, not reproducer:
      * Introduced by "observe:", "you will see:", "causes error:", "results in:", etc.
      * Contains no executable SQL/shell — only error text or stack traces.
      * Contains patterns like "ERROR:", "FATAL:", "panic:", "missing table=", etc.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUILDING THE REPRODUCER — extraction and synthesis rules
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Step 1 — Gather all executable code blocks from the "To Reproduce" section (or the
whole issue if no such section exists). Concatenate them in order; skip blocks that
are clearly error/output, not commands.

Step 2 — Read the surrounding PROSE for each step. If a step says something like
"do X in a way that causes Y failure" or "inject a failure" or "force a rollback"
or "observe the validation error", that prose is telling you the SCRIPT IS INCOMPLETE
— you must synthesize the missing SQL to actually trigger that failure mode.

Step 3 — Fill in every gap using the DB-specific synthesis patterns below. A reproducer
is only done when running it top-to-bottom on an UNFIXED binary actually fires the bug
and produces the buggy_output.

Step 4 — Add a verification query at the end when the bug is a corruption/silent-wrong-
result: the query should SELECT the value that exposes the bad state (invalid objects,
wrong count, orphaned descriptor, etc.).

Return NONE only for: startup/bootstrap crashes (use crash_on_startup instead); bugs
requiring external tools (BR, Lightning, PITR, TiFlash, network faults, physical
backup/restore); bugs needing a sharded cluster; or when there is genuinely no
executable form.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DB-SPECIFIC SYNTHESIS PATTERNS — use these to fill in missing steps
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

■ CockroachDB

  NEVER use crdb_internal or system tables in the reproducer — they are restricted in
  production CockroachDB ("Access to crdb_internal and system is restricted").
  Use information_schema equivalents instead:
    crdb_internal.invalid_objects       → information_schema.table_constraints
    crdb_internal.jobs / [SHOW JOBS]    → [SHOW JOBS] is fine (it is a CockroachDB extension)
    system.*                            → avoid entirely

  Legacy schema changer (bug is "in the legacy schema changer" or "legacy changer"):
    -- Goes in setup_preconditions (cluster-wide, run once before reproducer):
    SET CLUSTER SETTING sql.defaults.use_declarative_schema_changer = 'off';

  Schema change job fails / rolls back — CRITICAL RACE CONDITION:
    Schema change jobs for empty tables complete in milliseconds. A naive CANCEL JOBS
    will always get "not cancelable" because the job already finished. You MUST freeze
    job execution first so the job stays in 'pending' state long enough to cancel.

    TRIGGER DETECTION — apply this pattern whenever ANY of the following is true:
      • The issue title or body contains "rollback" near "schema change", "DDL", or "CREATE TABLE"
      • The bug description says a rollback/cancel path has a bug (e.g., "rollback leaves
        orphaned...", "rollback path does not clean up...", "missing cleanup in rollback")
      • The "To Reproduce" steps show CREATE TABLE with REFERENCES/FOREIGN KEY but give NO
        explicit mechanism to trigger a failure or rollback — the bug lives in the rollback
        path, which is NEVER exercised by the CREATE TABLE succeeding normally
      • The expected buggy output is a descriptor validation error, orphaned reference,
        or "backreference" error
    In ALL these cases: the literal CREATE TABLE sequence the issue shows is NOT a valid
    reproducer. That code shows the schema — the rollback path is the actual bug trigger,
    and you MUST synthesize the CANCEL JOBS mechanism to reach it.

    Full reliable schema-change-rollback pattern:
      -- In setup_preconditions (run once, persists across all iterations):
      SET CLUSTER SETTING sql.defaults.use_declarative_schema_changer = 'off';

      -- In reproducer (self-contained, runs in loop — manages async_exec_interval itself):
      SET CLUSTER SETTING sql.schema_changer.async_exec_interval = '1h';
      CREATE TABLE parent (id INT PRIMARY KEY);
      CREATE TABLE child (id INT PRIMARY KEY, parent_id INT REFERENCES parent(id));
      CANCEL JOBS (
        SELECT job_id FROM [SHOW JOBS]
        WHERE status = 'pending'
          AND description LIKE '%child%'
        ORDER BY created DESC LIMIT 1
      );
      SET CLUSTER SETTING sql.schema_changer.async_exec_interval = DEFAULT;
      SELECT constraint_name, constraint_type
      FROM information_schema.table_constraints
      WHERE table_name = 'parent' AND constraint_type = 'FOREIGN KEY';

    Why this works:
      - async_exec_interval = '1h' at the START of each iteration prevents the job
        scheduler from picking up the job, so it stays 'pending' and is always cancelable.
      - Canceling a 'pending' job triggers rollbackSchemaChange(), which is the buggy path.
      - Restoring DEFAULT at the END lets DROP DATABASE run between iterations.
      - The final SELECT shows orphaned FK back-references on 'parent' if the bug fired.
      - CRITICAL: async_exec_interval must be set INSIDE the reproducer (not only in
        setup_preconditions) so it is re-applied on every loop iteration. Setting it only
        in setup_preconditions means iteration 1 resets it to DEFAULT and iterations 2+
        can no longer freeze jobs, so CANCEL JOBS finds nothing to cancel.

  Transaction rollback (prose says "transaction is aborted", "rolled back"):
    BEGIN; ...; ROLLBACK;

  Wrong query result — EXPLAIN-based (issue shows bug via EXPLAIN plan comparison):
    EXPLAIN / EXPLAIN (VERBOSE) output is NOT the reproducer — it is evidence of the bug.
    You MUST synthesize a runnable reproducer that actually returns wrong data:
      1. Keep the schema DDL as-is.
      2. Extract the SELECT query by stripping the "EXPLAIN (VERBOSE)" prefix.
      3. Synthesize INSERT statements with rows chosen so the omitted/wrong filter
         changes the COUNT: e.g. if "tier IN (0, 1)" is wrongly dropped, insert a
         row with tier=2 — it passes on a buggy binary but is filtered on a fixed one.
      4. End the reproducer with: SELECT COUNT(*) FROM ... WHERE <the filter that the
         bug drops>; — this turns an invisible plan mistake into a concrete wrong number.
      5. Set buggy_output to the COUNT the buggy binary returns (e.g. "1").
      6. Set correct_output to what a fixed binary returns (e.g. "0").
      7. Set expected_output to the BUGGY count (same as buggy_output, e.g. "1").

  Wrong query result — runtime (SELECT returns wrong rows without EXPLAIN):
    End the reproducer with: SELECT COUNT(*) ... or SELECT <cols> ... that shows the wrong value.
    Set buggy_output / correct_output / expected_output to the concrete numeric values.

■ TiDB

  Schema change / DDL rollback (prose says "DDL job fails", "rollback DDL"):
    ADMIN CANCEL DDL JOBS <job_id>;
    -- or find the job: ADMIN SHOW DDL JOBS;

  Optimizer / wrong result (prose says "incorrect rows", "wrong result", "join reorder"):
    ANALYZE TABLE t;  -- ensure stats are present so the optimizer reorders
    SELECT ... (the query that returns the wrong result)
    Set expected_output to the BUGGY count/value.

  Disable optimization to compare:
    SET tidb_opt_enable_mpp = 0;  -- (or the relevant session variable)

■ MongoDB

  Use mongosh JavaScript. For collection-less aggregations:
    db.aggregate([{$documents: [...]}, {$group: ...}])
  For change streams (bug fires on consumer side):
    const cs = db.collection.watch([...]); cs.next();

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIELDS TO EXTRACT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. reproducer — the complete runnable script that triggers the bug.
   Must include every step: setup DDL, data insertion, the trigger mechanism
   (CANCEL JOBS, ROLLBACK, etc.), and a final verification query.
   Raw executable text only — no markdown fences, no numbered-step prose.

2. setup_preconditions — SQL/cluster-setting commands that must run BEFORE the
   main reproducer, while the cluster is in its initial state.
   Use for: cluster-wide settings (SET CLUSTER SETTING ...), feature flags,
   or seed state that must exist before the buggy image is active.
   null if the reproducer is fully self-contained.

3. buggy_output — verbatim output the REPRODUCER produces on an UNFIXED binary.
   Copy from "actual" / "we get" / "returns" / "got instead" / "observe the error".
   null if not spelled out.

4. correct_output — verbatim output a FIXED binary would produce.
   Copy from "expected" / "should return" / "correct answer".
   null if not spelled out.

5. expected_output — value the readiness probe greps for (tab-separated, no headers).
   For wrong-result bugs: the BUGGY value (probe matches when bug is active).
   null for crash/corruption bugs.

6. crash_on_startup — true only if the process fails on startup. false otherwise.

7. fault_injection_type — infrastructure action required BEYOND SQL to trigger the bug.
   - "node_kill": the bug only fires when a database node crashes mid-operation (e.g.
     a schema change job fails because the coordinator node dies — not because it was
     cancelled via SQL). Set this ONLY when the issue explicitly says the failure must
     come from a node crash / process kill / hardware failure, AND the SQL-only patterns
     above (CANCEL JOBS, ROLLBACK, etc.) cannot reproduce it.
   - null: bug is reproducible with SQL alone (default — use for 99% of bugs, including
     all CANCEL JOBS / ROLLBACK / wrong-result / crash-on-startup scenarios).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Your ENTIRE response must be a single JSON object. No prose before or after.
No markdown fences. Start with `{`, end with `}`, parseable by json.loads.

{
  "reproducer": "<complete runnable script, or NONE>",
  "setup_preconditions": "<pre-flight SQL/settings, or null>",
  "buggy_output": "<verbatim actual output, or null>",
  "correct_output": "<verbatim expected output, or null>",
  "expected_output": "<probe-grep value, or null>",
  "crash_on_startup": false,
  "fault_injection_type": null
}

Rules:
- NEVER put an error message, stack trace, or validation failure in reproducer.
- NEVER return a workaround, contrast case, expected-output display, or the fix.
- buggy_output / correct_output: copied verbatim — do not rephrase or reformat.
- When in doubt about a missing step: synthesize it using the patterns above.
  An incomplete reproducer that doesn't fire the bug is worse than a synthesized one.
"""

_USER_TEMPLATE = """\
Extract the reproducer, expected output, and crash_on_startup flag \
from this bug report. Return only a JSON object.

For expected_output: write the LITERAL rows the query should return \
(as mysql --batch --skip-column-names prints them: no headers, one row per line, \
tab-separated values). Do NOT write a description like "3 rows of 0" — write "0\\n0\\n0".

---
{body}
---
"""


_VALID_FAULT_INJECTION_TYPES = {"node_kill"}


def _llm_extract(body: str) -> tuple[str | None, str | None, str | None, str | None, str | None, bool, str | None]:
    """Return (reproducer, expected_output, buggy_output, correct_output, setup_preconditions, crash_on_startup, fault_injection_type) via LLM."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, None, None, None, None, False, None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        # Note: sonnet-4-6 rejects assistant-message prefill at request time, so
        # we rely on the strict "OUTPUT FORMAT" instruction in the system prompt
        # plus _extract_json's fence/prose tolerance to keep the output parseable.
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _USER_TEMPLATE.format(body=body[:6000])},
            ],
        )
        raw = _response_text(message)
        logger.warning(
            f"[ReproducerExtractor] LLM raw response "
            f"(stop_reason={getattr(message, 'stop_reason', '?')}): {raw[:500]!r}"
        )
        data = _extract_json(raw)
        if data is None:
            logger.warning("[ReproducerExtractor] LLM returned unparseable response — skipping extraction")
            return None, None, None, None, None, False, None
        reproducer = data.get("reproducer") or None
        if reproducer and reproducer.strip().upper() == "NONE":
            reproducer = None
        if reproducer and _is_prose(reproducer):
            logger.warning("[ReproducerExtractor] LLM returned prose steps — discarding")
            reproducer = None
        expected = data.get("expected_output") or None
        buggy_output = data.get("buggy_output") or None
        correct_output = data.get("correct_output") or None
        setup_preconditions = data.get("setup_preconditions") or None
        crash_on_startup = bool(data.get("crash_on_startup", False))
        fault_injection_type = data.get("fault_injection_type") or None
        if fault_injection_type not in _VALID_FAULT_INJECTION_TYPES:
            fault_injection_type = None
        if not reproducer and not crash_on_startup:
            logger.warning(f"[ReproducerExtractor] LLM returned no reproducer. Raw: {raw[:300]}")
        return (
            reproducer,
            expected,
            buggy_output,
            correct_output,
            setup_preconditions,
            crash_on_startup,
            fault_injection_type,
        )
    except Exception as e:
        logger.warning(f"[ReproducerExtractor] LLM extraction failed: {e}")
        return None, None, None, None, None, False, None


# ── LLM repair ────────────────────────────────────────────────────────────────

_REPAIR_SYSTEM_PROMPT = """\
You previously extracted a reproducer from a database bug report, but running it against
a live unfixed binary did NOT reproduce the bug. Produce a corrected reproducer.

OUTPUT FORMAT — read carefully:
Your ENTIRE response must be a single JSON object and nothing else. No prose before or
after. No markdown code fences. No explanation of your reasoning. The response must
start with `{` and end with `}` and be parseable by json.loads.

The object has exactly one key:
  { "reproducer": "<corrected executable text, or NONE if no fix is possible>" }

Silently consider these common failure modes when deciding on the fix:
- The issue text uses legacy-shell syntax that modern mongosh routes differently (e.g.
  `db.coll.insert(...)` is deprecated and goes through a bulk-write envelope that can
  inflate payloads past the 16 MB BSON limit — use `insertOne` / `insertMany` instead).
- You picked a contrast / workaround / expected-output block instead of the buggy one.
- The script needs additional setup (collection creation, seed docs) to reach the buggy
  code path.
- Statements run out of order, or in a shell that doesn't support the chosen syntax.
- The bug surfaces on a secondary/consumer side (change streams, getMore, oplog tailer)
  so the reproducer must actually read the event, not just perform the write.

Rules for the reproducer value:
- Preserve the intent of the original: same bug, same trigger, same surface.
- Raw executable text only — no markdown fences, no numbered-step prose, no comments
  explaining what it does.
- If the original was already correct and the failure is environmental, set reproducer
  to the string "NONE".
"""

_REPAIR_USER_TEMPLATE = """\
Original issue body:
---
{body}
---

Original reproducer (did NOT trigger the bug):
---
{reproducer}
---

Expected buggy output (what the description says an unfixed binary prints):
---
{buggy_output}
---

Expected correct output (what a fixed binary would print):
---
{correct_output}
---

Actual output observed when running the original reproducer:
---
{actual_output}
---

Return JSON: {{"reproducer": "<corrected text, or NONE>"}}
"""


def repair_reproducer(
    body: str,
    reproducer: str,
    actual_output: str,
    buggy_output: str | None,
    correct_output: str | None,
) -> str | None:
    """Ask the LLM to rewrite a reproducer that failed validation. Returns None if no repair is possible or the LLM is unavailable."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("[ReproducerExtractor] repair skipped: no ANTHROPIC_API_KEY")
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        # See _llm_extract: sonnet-4-6 rejects assistant-message prefill, so we
        # rely on the strict "OUTPUT FORMAT" instruction plus _extract_json.
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=_REPAIR_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _REPAIR_USER_TEMPLATE.format(
                        body=body[:6000],
                        reproducer=reproducer,
                        buggy_output=buggy_output or "(not extracted)",
                        correct_output=correct_output or "(not extracted)",
                        actual_output=actual_output[:2000],
                    ),
                },
            ],
        )
        raw = _response_text(message)
        logger.warning(
            f"[ReproducerExtractor] repair raw response "
            f"(stop_reason={getattr(message, 'stop_reason', '?')}): {raw[:500]!r}"
        )
        data = _extract_json(raw)
        if data is None:
            logger.warning("[ReproducerExtractor] repair returned unparseable response — no repair applied")
            return None
        repaired = data.get("reproducer") or None
        if repaired and repaired.strip().upper() == "NONE":
            return None
        if repaired and _is_prose(repaired):
            logger.warning("[ReproducerExtractor] repair returned prose — discarding")
            return None
        if repaired:
            ok, reason = _sanity_check_reproducer(repaired, buggy_output)
            if not ok:
                logger.warning(f"[ReproducerExtractor] repair rejected — {reason}")
                return None
        return repaired
    except Exception as e:
        logger.warning(f"[ReproducerExtractor] repair failed: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def extract_reproducer(body: str) -> tuple[str | None, str | None, bool]:
    """Return (reproducer, expected_output, crash_on_startup).

    Back-compat shim for callers that only need the three-field tuple.
    Prefer ``extract_reproducer_full`` when validation outputs are needed.
    """
    r, e, _b, _c, _s, c, _f = extract_reproducer_full(body)
    return r, e, c


def extract_reproducer_full(
    body: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None, bool, str | None]:
    """Return (reproducer, expected_output, buggy_output, correct_output, setup_preconditions, crash_on_startup, fault_injection_type).

    ``buggy_output`` and ``correct_output`` are the verbatim output snippets the
    description claims the reproducer produces on an unfixed vs. fixed binary.
    They are used by ``reproducer_validator`` to verify the extracted reproducer
    actually triggers the bug — they do NOT flow into the generated problem file.
    ``setup_preconditions`` is SQL / cluster-setting commands to run before the
    main reproducer (e.g. to force a schema change job to fail and roll back).
    ``fault_injection_type`` is non-null when the bug requires infrastructure
    disruption beyond SQL (currently only "node_kill").
    """
    if not body:
        return None, None, None, None, None, False, None

    if _is_sentry_auto_filed(body):
        logger.warning(
            "[ReproducerExtractor] Issue body indicates Sentry-auto-filed crash "
            "telemetry — no replayable reproducer possible, skipping extraction"
        )
        return None, None, None, None, None, False, None

    (
        reproducer,
        expected_output,
        buggy_output,
        correct_output,
        setup_preconditions,
        crash_on_startup,
        fault_injection_type,
    ) = _llm_extract(body)
    if not (reproducer or crash_on_startup):
        reproducer = _regex_extract(body)
        if reproducer:
            logger.debug(f"[ReproducerExtractor] Regex fallback found reproducer ({len(reproducer)} chars)")

    if reproducer:
        ok, reason = _sanity_check_reproducer(reproducer, buggy_output)
        if not ok:
            logger.warning(f"[ReproducerExtractor] Discarding extracted reproducer — {reason}")
            reproducer = None

    if reproducer or crash_on_startup or fault_injection_type:
        logger.debug(
            f"[ReproducerExtractor] result: reproducer={len(reproducer) if reproducer else 0}chars "
            f"expected_output={expected_output!r} buggy_output={buggy_output!r} "
            f"correct_output={correct_output!r} setup_preconditions={setup_preconditions!r} "
            f"crash_on_startup={crash_on_startup} fault_injection_type={fault_injection_type!r}"
        )
    return (
        reproducer,
        expected_output,
        buggy_output,
        correct_output,
        setup_preconditions,
        crash_on_startup,
        fault_injection_type,
    )
