"""
CloudThinker agent driver for SREGym.

Drives the two-phase benchmark flow (diagnosis + mitigation) against a locally
running CloudThinker stack, then submits results to the SREGym conductor.

Anna runs as a hosted multi-agent system, not a subprocess the harness owns, so
this driver uses `container_isolation: false` and talks to the stack through the
`ct` dev CLI (`ct chat --json`), which mints the JWT and streams the turn.

The SREGym MCP tools (kubectl / prometheus / loki / jaeger) are registered as
workspace MCP connections in CloudThinker, so Anna reaches the benchmark cluster
the same way a customer reaches their own -- no harness-side tool shim.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from clients.harness.problem_id import resolve_problem_id

# Add SREGym root to path
sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

logger = logging.getLogger("all.cloudthinker.driver")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

API_HOSTNAME = os.getenv("API_HOSTNAME", "localhost")
API_PORT = os.getenv("API_PORT", "8000")
CONDUCTOR_URL = f"http://{API_HOSTNAME}:{API_PORT}"

# `ct` resolves the repo root by walking up for docker-compose.yml + backend/,
# so it must run with the CloudThinker checkout as cwd -- not the SREGym root.
CT_BIN = os.environ.get("CT_BIN", "ct")
CT_REPO_DIR = os.environ.get("CT_REPO_DIR", "")
CT_WORKSPACE = os.environ.get("CT_WORKSPACE", "")
CT_SELECTION = os.environ.get("CT_SELECTION", "")
CT_TURN_TIMEOUT = int(os.environ.get("CT_TURN_TIMEOUT", "900"))
CT_MAX_APPROVALS = int(os.environ.get("CT_MAX_APPROVALS", "8"))
# There is no post-completion "window" to size a budget against -- that reading was
# wrong twice, and each time the fix was a bigger guess. What actually blocks the
# mitigation turn is the PREVIOUS turn still streaming: SREGym grades the diagnosis
# off the RCA run's REST status, which goes terminal minutes before the
# conversation's stream does, so the driver opens the next stage while Anna is
# still working. Measured on this VM 2026-07-31 (namespace_memory_limit): the judge
# graded at 11:19:16, the diagnosis stream ran to 11:23:31, and the 225s budget
# expired at ~11:23:02 -- 29 seconds short. The stage was recorded as a harness
# failure while the agent was mid-turn.
# So the wait is bounded by the only real ceiling a turn has, CT_TURN_TIMEOUT, and
# spent polling at a flat interval rather than on a backoff curve fitted to a
# number nobody measured. CT_RESUME_RETRIES still bounds NON-guard failures, which
# are genuine and must not be waited out.
CT_RESUME_RETRIES = int(os.environ.get("CT_RESUME_RETRIES", "9"))
CT_RESUME_BACKOFF = int(os.environ.get("CT_RESUME_BACKOFF", "5"))
CT_RESUME_POLL_SECONDS = int(os.environ.get("CT_RESUME_POLL_SECONDS", "15"))
# Substrings of the active-stream guard's body
# (app/use_cases/chat_stream_start_use_case.py CONVERSATION_BUSY_DETAIL, raised as
# HTTP 423). Matching the detail text rather than the bare status keeps an
# unrelated validation failure -- which is NOT worth waiting on -- out of the poll
# path. The first entry is the pre-2026-09 wording, kept so an older stack still
# matches; the guard moved from 400 to 423 and the text changed with it, and for
# one suite nothing matched, so every gate burned CT_RESUME_RETRIES backoffs.
CT_STREAM_BUSY_MARKERS = (
    "Conversation is already streaming",
    "This conversation is still finishing the previous response",
)
# A resume fired the instant a gate appears is rejected every single time, then
# succeeds ~5s later with the identical arguments. Measured across ~110 graded
# runs: the count of `Resume rejected` lines equals the count of approvals
# granted, exactly, at every gate depth (8 approvals/8 rejections, 5/5, 3/3,
# 2/2, 1/1). It is deterministic, not intermittent. On suite 0801_1224 the
# backend answered the too-early POST 200 and the SSE stream ended in 0s without
# ever subscribing to Pub/Sub; the retry 5.2s later subscribed and ran 16s.
# So wait BEFORE the first resume of each gate instead of spending a POST to
# discover the gate is not ready. This is not a substitute for the retry path
# below -- a rejection after the wait is still retried -- it only stops every
# gate burning retry #1 on a known-deterministic failure and logging it at
# ERROR. Set to 0 to restore the fire-first behaviour.
CT_GATE_SETTLE_SECONDS = int(os.environ.get("CT_GATE_SETTLE_SECONDS", "5"))
# A turn whose SSE stream dies mid-answer leaves a plausible-looking partial answer.
# Submitting that scores at the rubric floor and is indistinguishable from a real
# result, so retry the whole turn in a fresh conversation instead.
CT_TURN_RETRIES = int(os.environ.get("CT_TURN_RETRIES", "2"))
CT_TURN_BACKOFF = int(os.environ.get("CT_TURN_BACKOFF", "30"))
HARNESS_FAILURE_MARKER = "SREGYM_HARNESS_FAILURE"
# One of PROMPT_MODES below; unknown values fail at import rather than falling back.
# Required, with no default. run-problems.sh and agents.yaml both set it, and a
# third default here is what let a suite run one mode while its rows claimed
# another.
CT_PROMPT_MODE = os.environ.get("CT_PROMPT_MODE", "")

# Memory arm, set by run-problems.sh. The driver does not act on it -- the arm is
# implemented by what the chain wipes between problems -- but a cold and a warm
# score answer different questions, so the label and the recall state it was
# measured with belong in the log next to the model. `unset` means whoever ran
# this did not go through run-problems.sh, and the arm is unknown, not cold.
CT_MEMORY_ARM = os.environ.get("CT_MEMORY_ARM", "")
CT_MEMORY_SEEDED_OBSERVATIONS = os.environ.get("CT_MEMORY_SEEDED_OBSERVATIONS", "?")
CT_MEMORY_SEEDED_SKILLS = os.environ.get("CT_MEMORY_SEEDED_SKILLS", "?")
CT_MEMORY_SEEDED_MEMORIES = os.environ.get("CT_MEMORY_SEEDED_MEMORIES", "?")
CT_MEMORY_SEEDED_SCHEDULES = os.environ.get("CT_MEMORY_SEEDED_SCHEDULES", "?")
CT_RECENT_ACTIVITY_SEEDED = os.environ.get("CT_RECENT_ACTIVITY_SEEDED", "?")

# Which CloudThinker surface answers the diagnosis stage.
#
#   chat      Anna in a plain conversation, graded on whatever prose she printed.
#   rca_flow  Anna bound to an incident record, graded on the `analysis_md` she
#             persisted through `incident_root_cause`.
#
# These are different products, not two settings of one: the RCA lane has a
# hardcoded user prompt, its own model config, and a tool contract that REJECTS an
# analysis with no `**Origin:**` line. So an rca_flow number never averages with a
# chat number -- keep the lanes in separate results dirs and label every row.
#
# Mitigation is deliberately NOT forked. The RCA run exposes its `conversation_id`,
# and `ct chat -c` resumes it, so the fix turn is the same code on both lanes and
# any mitigation delta is attributable to the diagnosis that preceded it.
CT_LANE = os.environ.get("CT_LANE", "chat")
if CT_LANE not in {"chat", "rca_flow"}:
    raise SystemExit(f"CT_LANE must be 'chat' or 'rca_flow', got {CT_LANE!r}")
# Absolute, because this runs with the SREGym root as cwd while the script lives
# in the CloudThinker checkout and must import its sibling workspace module.
CT_RCA_SCRIPT = os.environ.get(
    "CT_RCA_SCRIPT",
    str(Path(CT_REPO_DIR or ".") / ".agents/skills/sregym-bench-internal/scripts/rca_run.py"),
)
# Fail at import, not at the first problem, and only on the lane that uses it. A default
# that names a moved file makes every run in the chain abort as a harness failure hours
# later, and the driver log that would have said so is zero-length.
if CT_LANE == "rca_flow" and not Path(CT_RCA_SCRIPT).is_file():
    raise SystemExit(f"CT_RCA_SCRIPT does not exist: {CT_RCA_SCRIPT}")
CT_RCA_PYTHON = os.environ.get("CT_RCA_PYTHON", str(Path(CT_REPO_DIR or ".") / "backend/.venv/bin/python3"))
CT_RCA_TIMEOUT = int(os.environ.get("CT_RCA_TIMEOUT", "1800"))

AGENT_LOGS_DIR = os.environ.get("AGENT_LOGS_DIR", "./logs/cloudthinker")


# ---------------------------------------------------------------------------
# CloudThinker via `ct chat`
# ---------------------------------------------------------------------------


def ct_chat(message: str, conversation_id: str | None = None, resume: bool = False) -> dict:
    """Run one Anna turn. Returns the `ct chat --json` summary object.

    On any failure returns a summary-shaped dict with an `error` set, so the
    driver can still submit something and let the run be scored rather than
    crashing the harness mid-problem.
    """
    cmd = [CT_BIN, "chat", message, "--json", "--timeout", str(CT_TURN_TIMEOUT)]
    if conversation_id:
        cmd += ["-c", conversation_id]
    if resume:
        cmd += ["--resume"]
    if CT_WORKSPACE:
        cmd += ["-w", CT_WORKSPACE]
    if CT_SELECTION:
        cmd += ["--selection", CT_SELECTION]

    logger.info(f"ct chat: {len(message)} chars, conversation={conversation_id or 'new'}")
    start = time.time()

    try:
        proc = subprocess.run(
            cmd,
            cwd=CT_REPO_DIR or None,
            capture_output=True,
            text=True,
            timeout=CT_TURN_TIMEOUT + 60,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"ct chat timed out after {CT_TURN_TIMEOUT + 60}s")
        return {"answer": "", "tool_calls": [], "error": "ct_timeout"}

    elapsed = int(time.time() - start)

    # `ct chat --json` prints one object; tolerate leading noise on stdout.
    summary = _parse_ct_json(proc.stdout)
    if summary is None:
        # stderr matters more than stdout here: when the backend rejects the
        # request outright `ct` prints nothing at all to stdout, so logging only
        # the stdout tail leaves an empty message and the real reason (an HTTP
        # status and detail) invisible until someone reads the backend's own log.
        logger.error(
            f"ct chat produced no JSON summary after {elapsed}s (rc={proc.returncode}). "
            f"stdout tail: {proc.stdout[-500:]!r} stderr tail: {proc.stderr[-500:]!r}"
        )
        # Carry the stderr tail so callers can tell the active-stream guard's 400
        # apart from a real failure. Without it every rejection collapses into a
        # bare `ct_no_json` and the resume loop cannot know whether it is waiting
        # on a healthy in-flight turn or retrying something already dead.
        return {
            "answer": "",
            "tool_calls": [],
            "error": "ct_no_json",
            "stderr": proc.stderr[-500:],
        }

    logger.info(
        f"ct chat done in {elapsed}s: status={summary.get('status')}, "
        f"answer={len(summary.get('answer') or '')} chars, "
        f"tool_calls={len(summary.get('tool_calls') or [])}"
    )

    # A non-zero exit means the stream never reached a terminal `complete` event,
    # so whatever text arrived is the opening of an answer Anna never finished.
    # Tag it rather than letting a caller mistake it for a finished turn.
    if proc.returncode != 0:
        logger.error(
            f"ct chat exited {proc.returncode} after {elapsed}s: "
            f"error={summary.get('error')!r} stderr tail: {proc.stderr[-500:]!r}"
        )
        # `stderr` on THIS path too, not only the no-JSON one above. `ct chat
        # --json` prints a summary whenever the SSE stream opened at all, so an
        # exit-6 rejection carries its reason in the JSON `error` field and
        # leaves stderr empty (measured: `ct chat exited 6 after 0s:` with a bare
        # tail, suite 0801_1224). The busy-vs-dead split reads `stderr`, so on
        # this path it was matching against a key that was never set and every
        # rejection fell through to the bounded-retry side. Setting it here, and
        # matching the summary's own error text in `_is_stream_busy`, is what
        # makes that documented split operative rather than decorative.
        summary["stderr"] = proc.stderr[-500:]
        summary["error"] = summary.get("error") or f"ct_exit_{proc.returncode}"

    return summary


def _parse_ct_json(stdout: str) -> dict | None:
    """Extract the trailing JSON object from `ct chat --json` output."""
    start = stdout.find("{")
    while start != -1:
        try:
            return json.loads(stdout[start:])
        except json.JSONDecodeError:
            start = stdout.find("{", start + 1)
    return None


_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def pending_approval(conversation_id: str | None) -> bool:
    """True when Anna's last message is parked on a HITL approval interrupt.

    `ct chat --json` reports `status=complete` for an interrupted turn, so the
    interrupt is only visible in the message row. `ct db query` is read-only.
    """
    if not conversation_id or not _UUID_RE.match(conversation_id):
        return False
    sql = (
        "select case when is_interrupt then 'PENDING' else 'CLEAR' end as gate "
        f"from message where conversation_id='{conversation_id}' "
        "order by created_at desc limit 1"
    )
    try:
        proc = subprocess.run(
            [CT_BIN, "db", "query", sql],
            cwd=CT_REPO_DIR or None,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ct db query timed out while checking for an approval gate")
        return False
    return "PENDING" in proc.stdout


def db_tool_calls(conversation_id: str | None) -> list[str]:
    """Tool names Anna actually executed, or `[]` when they cannot be read.

    Convenience wrapper for logging and stage artifacts, where an unreadable DB
    and a genuinely tool-free turn are equally uninteresting. Anything that acts
    on the difference -- the no-tool guard in `main` -- must call
    `read_tool_calls` and handle `None`.
    """
    return read_tool_calls(conversation_id) or []


def read_tool_calls(conversation_id: str | None) -> list[str] | None:
    """Tool names Anna actually executed, read from the message rows.

    `ct chat --json` returns an empty `tool_calls` list even for turns that ran
    dozens of tools, so the stage artifacts would otherwise carry no evidence of
    whether Anna touched the cluster.

    Returns `None` -- not `[]` -- when the read itself failed. The two mean
    opposite things to the caller: `[]` is evidence Anna never touched the
    cluster, `None` is evidence of nothing at all.
    """
    if not conversation_id or not _UUID_RE.match(conversation_id):
        return None
    # `skill_names` rides along because which skills Anna pulled in is part of the
    # harness a result ran under, not a detail: two runs on the same mode and model
    # loaded different skills (runbook §4.2), so a bare `load_skill` in the log
    # cannot tell those runs apart afterwards.
    sql = (
        "select mtc.tool_name, coalesce(mtc.tool_input->>'skill_names', '') as detail "
        "from messagetoolcomponent mtc "
        "join messagecomponent mc on mc.id = mtc.message_component_id "
        "join message m on m.id = mc.message_id "
        f"where m.conversation_id = '{conversation_id}' order by mtc.created_at"
    )
    try:
        proc = subprocess.run(
            [CT_BIN, "db", "query", sql],
            cwd=CT_REPO_DIR or None,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ct db query timed out while reading executed tool calls")
        return None

    if proc.returncode != 0:
        logger.warning(f"ct db query failed (rc={proc.returncode}) while reading executed tool calls")
        return None

    names = []
    for line in proc.stdout.splitlines():
        if not line.startswith("│"):
            continue
        cells = [c.strip() for c in line.strip("│ ").split("┆")]
        name = cells[0] if cells else ""
        if not name or name == "tool_name":
            continue
        detail = cells[1] if len(cells) > 1 else ""
        names.append(f"{name}({detail})" if detail else name)
    return names


# ---------------------------------------------------------------------------
# Session transcript
# ---------------------------------------------------------------------------

SESSION_FILENAME = "cloudthinker_session.json"
SESSION_SCHEMA = "cloudthinker_session/v1"
TRANSCRIPT_TIMEOUT = int(os.environ.get("CT_TRANSCRIPT_TIMEOUT", "120"))

# One row per message component, ordered by the message that owns it. The three
# component tables are disjoint and 1:1 with a message, so these LEFT JOINs never
# fan out; a user message owns no component row at all and carries its text in
# `message.content`.
_TRANSCRIPT_SQL = (
    "select m.id as message_id, m.role::text as role, m.content as message_content, "
    "m.created_at, mc.position, mc.type::text as component_type, "
    "mtxt.content as text, mth.content as thinking, mth.duration_ms as thinking_ms, "
    "mtc.tool_name, mtc.tool_input, mtc.tool_output, mtc.tool_reasoning, "
    "mtc.tool_runtime, mtc.tool_call_id, mtc.is_completed, "
    "mtc.approval_status::text as approval_status "
    "from message m "
    "left join messagecomponent mc on mc.message_id = m.id "
    "left join messagetextcomponent mtxt on mtxt.message_component_id = mc.id "
    "left join messagethinkingcomponent mth on mth.message_component_id = mc.id "
    "left join messagetoolcomponent mtc on mtc.message_component_id = mc.id "
    "where m.conversation_id = '{conversation_id}' and coalesce(m.is_deleted, false) = false "
    "order by m.created_at, mc.position, m.id"
)


def read_transcript(conversation_id: str | None) -> list[dict] | None:
    """Every message component of one conversation, in order, or `None` on a failed read.

    The conversation IS the agent session, so this is the source of the run's
    ATIF trajectory. `ct chat --json` returns only a summary of the last turn and
    `message.content` is empty on every agent row, so the transcript comes from
    the component tables the platform renders the chat from.

    Returns `None` -- not `[]` -- when the read failed, so a caller can tell
    "nothing was said" apart from "nothing could be read".
    """
    if not conversation_id or not _UUID_RE.match(conversation_id):
        return None
    sql = _TRANSCRIPT_SQL.format(conversation_id=conversation_id)
    try:
        proc = subprocess.run(
            [CT_BIN, "db", "query", "--json", sql],
            cwd=CT_REPO_DIR or None,
            capture_output=True,
            text=True,
            timeout=TRANSCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ct db query timed out while reading the session transcript")
        return None
    if proc.returncode != 0:
        logger.warning(f"ct db query failed (rc={proc.returncode}) while reading the session transcript")
        return None
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("ct db query returned unparsable JSON for the session transcript")
        return None
    return rows if isinstance(rows, list) else None


def write_session(logs_dir: Path, problem_id: str, stages: list[dict]) -> None:
    """Write the session that SREGym converts into an ATIF trajectory.

    `stages` is the ordered stage list, each entry `{name, conversation_id,
    submitted, records}` -- `records` being the transcript read when that stage
    submitted. The full transcript of every conversation is attached once under
    `conversations`: the mitigation stage resumes the diagnosis conversation, so
    repeating it per stage would duplicate the diagnosis in the trajectory.

    Best effort on purpose. A missing artifact costs a trajectory; an exception
    here would cost the run.
    """
    try:
        conversations: dict[str, dict] = {}
        for stage in stages:
            conversation_id = stage.get("conversation_id")
            if conversation_id and conversation_id not in conversations:
                conversations[conversation_id] = {
                    "conversation_id": conversation_id,
                    "records": read_transcript(conversation_id) or [],
                }
        session = {
            "schema": SESSION_SCHEMA,
            "problem_id": problem_id,
            "agent": "cloudthinker",
            "selection": CT_SELECTION,
            "prompt_mode": CT_PROMPT_MODE,
            "memory_arm": CT_MEMORY_ARM,
            "stages": [
                {
                    "name": stage.get("name"),
                    "conversation_id": stage.get("conversation_id"),
                    "submitted": stage.get("submitted"),
                    "rows_at_submission": stage.get("rows_at_submission"),
                }
                for stage in stages
            ],
            "conversations": list(conversations.values()),
        }
        path = logs_dir / SESSION_FILENAME
        path.write_text(json.dumps(session, indent=2, default=str))
        logger.info(f"Saved session transcript to {path}")
    except Exception as exc:  # noqa: BLE001 - an artifact is never worth the run
        logger.warning(f"Could not write {SESSION_FILENAME}: {exc}")


def _is_stream_busy(summary: dict) -> bool:
    """True when a rejection means "the previous turn is still alive", not "dead".

    The two failures share one error string and need opposite policies -- wait on
    a live stream, retry a dead attempt -- so this predicate is the split, and
    both call sites must use it rather than re-deriving the test.

    It reads BOTH fields because `ct` reports the same guard two different ways.
    A rejection at stream START prints nothing to stdout and puts the 400 detail
    on stderr. A rejection once the stream opened prints a JSON summary whose
    `error` field carries the detail and leaves stderr empty. Checking only
    `stderr`, as this used to, made the split inoperative on the second shape.
    """
    haystack = f"{summary.get('stderr') or ''}\n{summary.get('error') or ''}"
    return any(marker in haystack for marker in CT_STREAM_BUSY_MARKERS)


def run_turn(message: str, conversation_id: str | None = None) -> dict:
    """One Anna turn, auto-approving any HITL gates she hits along the way.

    A benchmark cluster has no human approver, so the driver stands in for the
    approve click. Returns a summary whose `answer` concatenates every segment
    and whose `tool_calls` spans the whole turn, plus `approvals` for the count.
    """
    summary = ct_chat(message, conversation_id=conversation_id)
    conversation_id = summary.get("conversation_id") or conversation_id
    answers = [summary.get("answer") or ""]
    calls = list(summary.get("tool_calls") or [])
    approvals = 0

    retries = 0
    busy_waited = 0

    while approvals < CT_MAX_APPROVALS and pending_approval(conversation_id):
        logger.info(f"Approval gate {approvals + 1} hit — auto-approving and resuming")
        # Only before the FIRST attempt at this gate. A retry has already paid
        # its own backoff, so settling again would double the wait.
        if retries == 0 and busy_waited == 0 and CT_GATE_SETTLE_SECONDS:
            time.sleep(CT_GATE_SETTLE_SECONDS)
        resumed = ct_chat(
            "Approved. Proceed and apply the change.",
            conversation_id=conversation_id,
            resume=True,
        )
        # Same active-stream guard as `resume_turn_retrying`, same split: a busy
        # conversation is a live previous turn and is waited out on the clock,
        # everything else is a real failure and is retried a bounded number of
        # times. Neither burns a gate. Keeping one policy in both places is
        # deliberate -- the earlier version had this call site on a backoff curve
        # sized for a window that does not exist.
        if resumed.get("error") or resumed.get("status") == "error":
            if _is_stream_busy(resumed):
                # Bounded by the same ceiling a turn has. Without this the loop
                # would spin forever on a conversation that never goes idle,
                # because a busy poll deliberately does not count as a failure.
                if busy_waited >= CT_TURN_TIMEOUT:
                    logger.error(f"{conversation_id} still streaming after {busy_waited}s — abandoning approvals")
                    break
                busy_waited += CT_RESUME_POLL_SECONDS
                logger.info(
                    f"{conversation_id} still streaming before the approval "
                    f"resume — waiting {CT_RESUME_POLL_SECONDS}s "
                    f"({busy_waited}s of {CT_TURN_TIMEOUT}s spent)"
                )
                time.sleep(CT_RESUME_POLL_SECONDS)
                continue
            retries += 1
            if retries > CT_RESUME_RETRIES:
                logger.error(f"Resume failed {retries}x — abandoning approvals")
                break
            logger.warning(f"Resume rejected (attempt {retries}, {resumed.get('error')!r}) — backing off")
            time.sleep(CT_RESUME_BACKOFF * retries)
            continue
        # Budget is per gate, not per stage: the lock is hit once per gate, so a
        # 6-gate mitigation would otherwise exhaust CT_RESUME_RETRIES and abandon
        # approvals halfway through applying the fix. The busy clock resets for
        # the same reason -- each gate waits on its own preceding turn.
        retries = 0
        busy_waited = 0
        approvals += 1
        answers.append(resumed.get("answer") or "")
        calls += list(resumed.get("tool_calls") or [])
        summary = resumed

    if approvals >= CT_MAX_APPROVALS and pending_approval(conversation_id):
        logger.warning(f"Still gated after {approvals} approvals — giving up on this turn")

    return {
        **summary,
        "conversation_id": conversation_id,
        "answer": "\n\n".join(a for a in answers if a),
        "tool_calls": calls,
        "approvals": approvals,
    }


def run_turn_retrying(message: str) -> dict:
    """A diagnosis turn that refuses to hand back a half-finished answer.

    A backend restart mid-turn (hot reload, OOM kill, deploy) kills the SSE relay
    and `ct` exits non-zero holding Anna's opening sentence. Submitting that text
    scores at the rubric floor, which reads exactly like a weak-but-real answer in
    the results CSV. Retry the whole turn in a *fresh* conversation — the dead one
    still holds the per-conversation streaming lock — and only then give up.
    """
    for attempt in range(1, CT_TURN_RETRIES + 2):
        summary = run_turn(message)
        if not summary.get("error"):
            return summary
        logger.error(
            f"Turn attempt {attempt} failed ({summary['error']}), "
            f"answer={len(summary.get('answer') or '')} chars — discarding it"
        )
        if attempt <= CT_TURN_RETRIES:
            time.sleep(CT_TURN_BACKOFF)
    logger.error(f"{HARNESS_FAILURE_MARKER}: turn failed {CT_TURN_RETRIES + 1}x, no valid answer")
    return {**summary, "harness_failure": True}


def resume_turn_retrying(message: str, conversation_id: str) -> dict:
    """Continue an existing conversation, waiting out a stream that is still open.

    Distinct from `run_turn_retrying`, which recovers by starting a FRESH
    conversation. That is exactly wrong here: the mitigation turn only works
    because it continues the diagnosis conversation, so a retry that abandons it
    would silently turn the stage into a cold turn with none of the investigation
    in context.

    What we are waiting out is the same active-stream guard the approval loop in
    `run_turn` already backs off from --

        POST /autonomous-agents/chat/stream?conversation_id=...
        400 "Conversation is already streaming. Please reload the page to reconnect."
        (app/use_cases/chat_stream_start_use_case.py, active-stream guard)

    -- so this reuses that loop's budget rather than inventing a second policy.
    The gap was only that the guard was handled for gate resumes and not for the
    turn that opens the mitigation stage.

    `ct` prints nothing to stdout on that rejection, so it surfaces as `ct_no_json`
    in about 60ms with the guard's detail on stderr. Two very different failures
    share that one error string, and giving them one policy is what kept breaking
    this: the guard means "the previous turn is STILL RUNNING, keep waiting", while
    anything else means "this attempt is dead, retry a bounded number of times".
    Waiting out a dead attempt wastes the stage; giving up on a live one throws
    away work the agent is in the middle of doing. Both happened.

    Cancelling the in-flight stream instead of waiting is tempting and wrong. On
    2026-07-31 the stream this loop was blocked on turned out to be the mitigation
    itself: it ran 787s, executed 27 further tools, and the oracle scored it
    `SERVICE_OK`. A pre-emptive cancel would have destroyed a passing run.
    """
    deadline = time.time() + CT_TURN_TIMEOUT
    failures = 0
    while True:
        summary = run_turn(message, conversation_id=conversation_id)
        if not summary.get("error"):
            return summary

        if _is_stream_busy(summary):
            remaining = int(deadline - time.time())
            if remaining <= 0:
                logger.error(
                    f"{HARNESS_FAILURE_MARKER}: {conversation_id} was still "
                    f"streaming after {CT_TURN_TIMEOUT}s — mitigation never "
                    "reached the agent"
                )
                break
            logger.info(
                f"{conversation_id} is still streaming its previous turn — "
                f"waiting {CT_RESUME_POLL_SECONDS}s ({remaining}s left)"
            )
            time.sleep(CT_RESUME_POLL_SECONDS)
            continue

        failures += 1
        logger.error(
            f"Resume attempt {failures} on {conversation_id} failed "
            f"({summary['error']}), answer={len(summary.get('answer') or '')} chars"
        )
        if failures > CT_RESUME_RETRIES:
            logger.error(
                f"{HARNESS_FAILURE_MARKER}: resume of {conversation_id} failed "
                f"{failures}x — mitigation never reached the agent"
            )
            break
        time.sleep(CT_RESUME_BACKOFF * failures)

    return {**summary, "harness_failure": True}


def rca_flow_diagnosis(message: str, logs_dir: Path) -> dict:
    """Diagnose through the incident RCA lane instead of a chat turn.

    Returns the same shape `run_turn_retrying` does, so the caller's submission,
    tool-call audit and mitigation stage are untouched -- only the surface that
    produced the answer differs.

    The graded text is `root_cause_md` + `analysis_md`, both of them, because
    both are what the incident record actually shows a reader: the summary carries
    the confidence framing and the analysis carries the origin line and evidence.
    Submitting only one would handicap the lane for no product reason.

    `--json-out` is what keeps a bad answer distinguishable from a broken harness.
    A run that finishes with no analysis is a real lane failure and must reach the
    judge as a weak answer; only a run that never reaches a terminal state, or a
    script that dies, is a harness failure.
    """
    if not CT_WORKSPACE:
        logger.error(f"{HARNESS_FAILURE_MARKER}: CT_LANE=rca_flow needs CT_WORKSPACE set")
        return {"harness_failure": True, "error": "CT_WORKSPACE unset"}

    prompt_path = logs_dir / "rca_incident_body.txt"
    prompt_path.write_text(message)
    json_path = logs_dir / "rca_run.json"
    cmd = [
        CT_RCA_PYTHON,
        CT_RCA_SCRIPT,
        CT_WORKSPACE,
        "--title",
        "SREGym benchmark incident",
        "--description-file",
        str(prompt_path),
        "--json-out",
        str(json_path),
        "--timeout",
        str(CT_RCA_TIMEOUT),
    ]
    logger.info(f"RCA lane: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path(CT_RCA_SCRIPT).parent),
            capture_output=True,
            text=True,
            timeout=CT_RCA_TIMEOUT + 120,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"{HARNESS_FAILURE_MARKER}: RCA lane exceeded {CT_RCA_TIMEOUT}s")
        return {"harness_failure": True, "error": "rca timeout"}

    for line in (proc.stdout or "").splitlines():
        logger.info(f"rca| {line}")
    if proc.returncode != 0 or not json_path.exists():
        logger.error(f"{HARNESS_FAILURE_MARKER}: RCA lane rc={proc.returncode}")
        logger.error((proc.stderr or "")[-2000:])
        return {"harness_failure": True, "error": f"rca rc={proc.returncode}"}

    run = json.loads(json_path.read_text())
    logger.info(
        f"RCA run {run.get('rca_run_id')} status={run.get('status')} "
        f"confidence={run.get('confidence_score')} origin={run.get('origin')!r}"
    )
    answer = "\n\n".join(part for part in (run.get("root_cause_md"), run.get("analysis_md")) if part)
    return {
        "answer": answer,
        "conversation_id": run.get("conversation_id"),
        "tool_calls": [],
        "approvals": 0,
        "rca_origin": run.get("origin"),
        "rca_status": run.get("status"),
        "rca_confidence": run.get("confidence_score"),
    }


def tool_names(summary: dict) -> list[str]:
    """Flatten tool_calls into names, tolerating both str and dict entries."""
    names = []
    for call in summary.get("tool_calls") or []:
        if isinstance(call, str):
            names.append(call)
        elif isinstance(call, dict):
            names.append(str(call.get("name") or call.get("tool") or call))
    return names


# ---------------------------------------------------------------------------
# SREGym conductor helpers
# ---------------------------------------------------------------------------


def get_app_info(max_retries: int = 6, backoff: int = 5) -> dict:
    """Fetch application info from conductor."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/get_app", timeout=10)
            resp.raise_for_status()
            info = resp.json()
            logger.info(f"App info: {info}")
            return info
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"get_app attempt {attempt}/{max_retries} failed: {e}")
                time.sleep(backoff)
            else:
                raise


def wait_for_stage(target_stages: set[str], timeout: int = 300) -> str:
    """Poll conductor until stage is in target_stages."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/status", timeout=10)
            resp.raise_for_status()
            stage = resp.json().get("stage", "")
            if stage in target_stages:
                logger.info(f"Conductor reached stage: {stage}")
                return stage
        except Exception as e:
            logger.debug(f"Status poll error: {e}")
        time.sleep(2)

    raise TimeoutError(f"Conductor did not reach {target_stages} within {timeout}s")


def submit_to_conductor(solution: str) -> None:
    """POST /submit to conductor."""
    logger.info(f"Submitting to conductor ({len(solution)} chars)")
    resp = requests.post(f"{CONDUCTOR_URL}/submit", json={"solution": solution}, timeout=30)
    if not resp.ok:
        logger.error(f"Submit failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    logger.info(f"Submit response: {resp.json()}")


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# CT_PROMPT_MODE selects a *pair* of prompts, diagnosis and mitigation, not just
# the diagnosis one. A mode is the unit a published number is labelled with, so
# changing either half of a shipped mode would silently invalidate results already
# recorded under that label. Older modes are therefore frozen, never edited.
#
# `guided` is the original. It was written before we read the judge rubric and
# independently reproduced it: "the actual mutated value: the selector, env var,
# image tag, port, ... resource limit" is D2-Q2 almost verbatim, and "distinguish
# the resource that caused the failure from resources that merely show symptoms"
# is D1-Q2. Scores under it measure Anna with the answer key in hand, so they are
# not comparable to any published baseline. Frozen: it reproduces the 2026-07-27
# pilot.
#
# `neutral` matches the harness's own Claude Code driver
# (clients/claudecode/driver.py:149-189), written by the SREGym authors:
# investigate, then submit prose describing the issue, graded on whether the
# faulty components and root cause are right. No enumeration of which fields might
# be mutated, no origin-versus-symptom hint. The only deliberate divergence is
# naming our MCP connections, which is plumbing, not guidance. Frozen: it
# reproduces the 2026-07-28 cold n=3.
#
# `scoped` is the default, and is `neutral` plus exactly two additions. Both were
# decided deliberately on 2026-07-28 (runbook §3.3, §3.4); they are not neutrality
# fixes and must not be read as such.
#
#   1. Diagnosis names SREGym's own telemetry plumbing as out of scope. The
#      observer rewrites `jaeger*` into ExternalName aliases for `otel-collector`
#      in every namespace of all 116 problems and orphans a jaeger pod, so a
#      service-with-no-matching-pods exists regardless of the injected fault --
#      the same shape as several real faults. This is a divergence from the
#      upstream baseline, which gets no such warning: it removes a distractor
#      SREGym never intended to inject, at the cost of no longer measuring whether
#      an agent can tell harness scaffolding from the application. It says nothing
#      about where the fault is or how to look for it, and deliberately does NOT
#      say "check the app's entry-point service first" -- anchoring on the first
#      dramatic signal instead of the front door is Anna's real weakness and stays
#      measured.
#
#   2. Mitigation states the grading criterion. Upstream already tells its agent
#      "Your fix is also evaluated on whether it addresses the root cause, not
#      just the symptoms"; ours stated no criterion at all, so this closes a gap
#      rather than opening one. It is what the §3.4 revert-versus-workaround
#      question reduces to once the upstream wording is the reference.
#
# Every mode keeps the autonomy instruction. Upstream has the same ("DO NOT ask
# for user confirmation"), and without it our HITL gate deadlocks the run instead
# of measuring anything.

DIAGNOSIS_PROMPT_NEUTRAL = """You are an SRE agent tasked with diagnosing issues in a Kubernetes application.

Application: {app_name}
Namespace: {namespace}
{descriptions}

CRITICAL: You are running in an AUTOMATED environment. Work autonomously and make all
decisions yourself. DO NOT ask for user confirmation or approval. Proceed based on your
own analysis.

Investigate the application to detect any anomalies or issues. Analyse metrics, logs and
traces. Your MCP connections are already wired to this cluster:
- `k8s` — a Kubernetes connection; run `kubectl` in your sandbox, KUBECONFIG is set
- `sgprom` — Prometheus metrics and alerts

When ready, report a natural language description of the issue you found.

Your diagnosis is evaluated on whether you correctly identify the faulty components and
the root cause."""

DIAGNOSIS_PROMPT_GUIDED = """You are on call for a Kubernetes application that is failing.

Application: {app_name}
Namespace: {namespace}
Description: {descriptions}

Investigate the live cluster and determine the root cause. Your MCP connections
are already wired to this cluster:
- `k8s` — a Kubernetes connection; run `kubectl` in your sandbox, KUBECONFIG is set
  (pods, events, describe, manifests)
- `sgprom` — Prometheus metrics and alerts

Do not ask me for approval or for more information. Investigate directly.

Report your diagnosis as plain prose covering:
- which specific resource is at fault, named exactly (kind/name)
- what is concretely wrong with it — the actual mutated value: the selector,
  env var, image tag, port, replica count or resource limit, and what it should be
- which other components are affected downstream, and how users experience it

Distinguish the resource that caused the failure from resources that merely show
symptoms. Be specific and technical; cite the evidence you found."""

# `scoped` = `neutral` + notes about artifacts of the environment rather than of the
# fault. Built by concatenation so the two can never drift apart: whatever `neutral`
# says, the only differences `scoped` introduces are the paragraphs below.
#
# The tooling paragraph is a workaround for a CloudThinker defect, not a benchmark one:
# the `monitoring-*` connection skills are baked into every sandbox image and `load_skill`
# resolves them by name with no check that the workspace has the matching connection, so
# Anna can load `monitoring-prometheus` here even though nothing in her system prompt
# offers it (skills are matched on `connection_type == prefix`, and our observability
# ones are `sgprom`, `sgloki`, `sgjaeger`). Their scripts then import
# `@connections/prometheus`,
# which does not exist -- the generated module is `@connections/sgprom` -- and die on
# `Cannot find module`. Observed 2026-07-29: one wasted `load_skill` plus two failed
# `bun run`s, and a page of instructions for four unusable scripts left in her context.
# Remove this paragraph once `load_skill` gates on the workspace's own connections.
DIAGNOSIS_PROMPT_SCOPED = (
    DIAGNOSIS_PROMPT_NEUTRAL
    + """

Scope: the benchmark harness installs its own telemetry plumbing into every namespace it
runs. The `jaeger`, `jaeger-agent`, `jaeger-collector` and `jaeger-query` Services are
ExternalName aliases for `otel-collector`. Do not report those four Services as the issue
and do not modify them.

Tooling: Kubernetes is a standard connection -- just run `kubectl` in your sandbox, its
KUBECONFIG is already exported. The three observability connections are MCP connections,
so the bundled `monitoring-*` skills do not apply to them and their scripts will not run.
Do not `load_skill` any of them. Call each connection's own tools directly instead."""
)

# `scoped_rca` = `scoped` + one line directing Anna to her own RCA skill. Added
# 2026-07-29.
#
# This adds no capability she lacked. `root-cause-analysis` is a public skill, and
# public skills are listed in every system prompt as name + description
# (prompts/shared/tools_context.py); its description -- "use when investigating why
# something is broken, slow, failing or alerting ... and the cause is not already
# obvious from one piece of evidence" -- describes this task almost word for word.
# She sees it every turn. What this mode removes is her *choice* about using it.
#
# That choice is why this is a separate mode and never an edit to `scoped`. On
# 2026-07-28 the two Sonnet runs loaded the skill and the two Opus runs did not
# (conversations db752330/5215cc82 vs e8efdced/850efac5) -- same mode, same prompt.
#
# Standard mode as of 2026-07-29, Henry's call: every published number is `scoped_rca`,
# and `scoped` is no longer run. Two consequences to state plainly rather than discover
# later.
#
# 1. The gap `scoped` vs `scoped_rca` was the only measurement of how reliably skill
#    auto-discovery fires on an RCA-shaped request, and we are no longer taking it. The
#    nondeterminism above is real and now goes unmeasured here; if it matters it needs a
#    test outside this benchmark.
# 2. A real user never receives this directive. So a `scoped_rca` number describes
#    CloudThinker with its RCA skill guaranteed loaded, not CloudThinker as a customer
#    meets it. Label published results accordingly.
#
# Leakage: the directive names a skill, not a finding. `root-cause-analysis` holds
# generic discipline -- competing hypotheses, evidence standards, causal chain -- and
# none of the rubric's mutated-field vocabulary (env var, port, selector, image tag,
# resource limit) that makes `guided` unpublishable. It is closer to naming an MCP
# connection than to `guided`. Still a divergence from upstream, which does not tell
# its agent which of its own skills to read.
DIAGNOSIS_PROMPT_SCOPED_RCA = (
    DIAGNOSIS_PROMPT_SCOPED
    + """

Before you start investigating, run `load_skill` for the `root-cause-analysis` skill and
follow the method it describes. It carries this organisation's general investigation
    discipline and says nothing about this application or this fault."""
)

DIAGNOSIS_PROMPT_SCOPED_RCA_CORPUS = (
    DIAGNOSIS_PROMPT_SCOPED_RCA
    + """

If useful, scout `./_skills/public/root-cause-analysis/incidents/` for a matching platform failure pattern, then verify any match against live evidence before using it."""
)

MITIGATION_PROMPT_BASE = """Now fix it.

Application: {app_name}
Namespace: {namespace}

You have write access to the cluster. Apply the fix yourself by running
`kubectl` in your sandbox (patch, edit, scale, rollout, apply). This is a
disposable benchmark cluster, you are authorised to
change it, and no human is available to approve anything — do not stop to ask,
and do not merely describe what you would do.

Then verify: pods Running, containers Ready, and the symptom you identified is
gone. Report what you changed and the evidence that it worked."""

# Upstream's wording, minus its submission mechanics (clients/claudecode/driver.py:163-165).
MITIGATION_PROMPT_SCOPED = (
    MITIGATION_PROMPT_BASE
    + """

Your fix is evaluated on whether the application is healthy after your changes, and on
whether it addresses the root cause rather than only the symptoms."""
)

# One mode, one prompt pair. Adding a mode is how a prompt change ships; editing a
# shipped mode silently rewrites the meaning of every result already labelled with it.
PROMPT_MODES = {
    "guided": (DIAGNOSIS_PROMPT_GUIDED, MITIGATION_PROMPT_BASE),
    "neutral": (DIAGNOSIS_PROMPT_NEUTRAL, MITIGATION_PROMPT_BASE),
    "scoped": (DIAGNOSIS_PROMPT_SCOPED, MITIGATION_PROMPT_SCOPED),
    "scoped_rca": (DIAGNOSIS_PROMPT_SCOPED_RCA, MITIGATION_PROMPT_SCOPED),
    "scoped_rca_corpus": (DIAGNOSIS_PROMPT_SCOPED_RCA_CORPUS, MITIGATION_PROMPT_SCOPED),
}

if not CT_PROMPT_MODE:
    raise SystemExit("CT_PROMPT_MODE is required; run-problems.sh sets it")
if CT_PROMPT_MODE not in PROMPT_MODES:
    raise SystemExit(f"CT_PROMPT_MODE={CT_PROMPT_MODE!r} is not one of {sorted(PROMPT_MODES)}")

DIAGNOSIS_PROMPT, MITIGATION_PROMPT = PROMPT_MODES[CT_PROMPT_MODE]


# ---------------------------------------------------------------------------
# Main driver loop
# ---------------------------------------------------------------------------


def main():
    logs_dir = Path(AGENT_LOGS_DIR)
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(logs_dir / "driver.log")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(file_handler)

    logger.info("=" * 60)
    logger.info("CloudThinker driver starting")
    logger.info(f"Conductor: {CONDUCTOR_URL}")
    logger.info(f"ct: {CT_BIN} (cwd={CT_REPO_DIR or 'inherited'}, workspace={CT_WORKSPACE or 'default'})")
    # Both prompts come from this mode, so the log line labels the whole run.
    logger.info(f"Prompt mode: {CT_PROMPT_MODE}")
    logger.info(f"Selection: {CT_SELECTION or '<backend preference>'}")
    # Seeded counts, not just the arm: a warm run that could recall nothing is a
    # cold run, and this line is the only place that distinction survives.
    logger.info(
        f"Lane: {CT_LANE} | Memory arm: {CT_MEMORY_ARM or 'unset'} "
        f"(seeded: {CT_MEMORY_SEEDED_OBSERVATIONS} observations, "
        f"{CT_MEMORY_SEEDED_SKILLS} skills, "
        f"{CT_MEMORY_SEEDED_MEMORIES} memory files, "
        f"{CT_MEMORY_SEEDED_SCHEDULES} scheduled tasks, "
        f"{CT_RECENT_ACTIVITY_SEEDED} recent activity entries)"
    )
    logger.info("=" * 60)

    if not CT_REPO_DIR:
        logger.warning("CT_REPO_DIR unset — `ct` must find the CloudThinker repo from the inherited cwd")

    try:
        wait_for_stage({"diagnosis"}, timeout=300)
    except TimeoutError:
        logger.error("Timed out waiting for conductor to reach diagnosis stage")
        sys.exit(1)

    app_info = get_app_info()
    problem_id = resolve_problem_id()
    logger.info(f"Problem ID (harness): {problem_id}")

    params = {
        "app_name": app_info.get("app_name", "unknown"),
        "namespace": app_info.get("namespace", "default"),
        "descriptions": app_info.get("descriptions", ""),
    }

    # ================================================================
    # PHASE 1: DIAGNOSIS
    # ================================================================
    logger.info("=" * 40 + " DIAGNOSIS " + "=" * 40)

    if CT_LANE == "rca_flow":
        diagnosis = rca_flow_diagnosis(DIAGNOSIS_PROMPT.format(**params), logs_dir)
    else:
        diagnosis = run_turn_retrying(DIAGNOSIS_PROMPT.format(**params))
    conversation_id = diagnosis.get("conversation_id")

    if diagnosis.get("harness_failure"):
        abort_harness_failure(
            logs_dir,
            problem_id,
            diagnosis,
            reason=f"the agent harness could not complete a turn ({diagnosis.get('error')})",
        )

    diagnosis_text = diagnosis.get("answer") or "Unable to complete diagnosis"

    # Read from the DB, not the summary: `tool_names` is near-always empty (see
    # read_tool_calls), and an empty list here reads exactly like an answer Anna
    # invented without touching the cluster. That false signal is what made a
    # genuine 100/100 run look like memory recall.
    executed = read_tool_calls(conversation_id)
    logger.info(f"Diagnosis tools used: {executed if executed is not None else '<unreadable>'}")

    # Zero executed tools is an infrastructure failure, never a weak answer.
    # Anna cannot see a live cluster except through her MCP connections, so a
    # diagnosis produced without one tool call was written blind -- and the only
    # ways to be blind are the connections being down or the run being answered
    # from memory, both of which invalidate the result rather than score it.
    # Observed 2026-07-29: a machine restart reverted the OpenSandbox egress
    # NetworkPolicy, all four connections went ERROR, Anna submitted "I am
    # blocked" prose, and the suite recorded 0.0 with rc=0 and no error anywhere.
    # Unattended, that silently poisons every problem after the one that broke.
    #
    # `None` is deliberately not caught: an unreadable DB is not evidence.
    if executed == []:
        abort_harness_failure(
            logs_dir,
            problem_id,
            diagnosis,
            reason=(
                "the agent executed zero tools during diagnosis — its MCP connections were "
                "almost certainly down (check the OpenSandbox egress NetworkPolicy, runbook §2.3)"
            ),
        )

    submit_to_conductor(diagnosis_text)
    _save_stage_result(logs_dir, "diagnosis", diagnosis)

    # Watermark for the diagnosis half of the conversation. The mitigation stage
    # resumes this same conversation, so the row count at submission is the only
    # place the diagnosis->mitigation boundary exists.
    diagnosis_records = read_transcript(conversation_id)
    stages = [
        {
            "name": "diagnosis",
            "conversation_id": conversation_id,
            "submitted": diagnosis_text,
            "rows_at_submission": len(diagnosis_records) if diagnosis_records is not None else None,
        }
    ]
    logger.info(
        "Diagnosis transcript rows at submission: "
        f"{stages[0]['rows_at_submission'] if stages[0]['rows_at_submission'] is not None else '<unreadable>'}"
    )

    # This wait covers the conductor grading the diagnosis with a remote LLM
    # judge, which is not the agent's clock -- a slow judge should not cost the
    # mitigation stage of an otherwise valid run. Polling exits the moment the
    # stage flips, so a generous ceiling costs nothing when the judge is healthy.
    try:
        stage = wait_for_stage({"mitigation", "done"}, timeout=int(os.getenv("CT_STAGE_WAIT", "900")))
    except TimeoutError:
        logger.error("Timed out waiting for mitigation stage")
        sys.exit(1)

    if stage == "done":
        logger.info("Conductor went straight to done after diagnosis")
        write_session(logs_dir, problem_id, stages)
        _finish(logs_dir, problem_id)
        return

    # ================================================================
    # PHASE 2: MITIGATION
    # ================================================================
    logger.info("=" * 40 + " MITIGATION " + "=" * 39)

    # Same conversation, so Anna keeps her own diagnosis in context -- and so a
    # retry must never fall back to a fresh one. `resume_turn_retrying` waits out
    # the conversation's still-open stream instead of recording an instant failure.
    mitigation = resume_turn_retrying(MITIGATION_PROMPT.format(**params), conversation_id=conversation_id)
    approvals = mitigation.get("approvals", 0)
    # Whole-conversation list, so it includes the diagnosis tools too.
    logger.info(f"Tools after mitigation: {db_tool_calls(conversation_id)} (approvals granted: {approvals})")

    # Empty solution: the fix is applied in-cluster, the oracle checks the cluster.
    submit_to_conductor("")
    _save_stage_result(logs_dir, "mitigation", mitigation)
    stages.append(
        {
            "name": "mitigation",
            "conversation_id": mitigation.get("conversation_id") or conversation_id,
            "submitted": mitigation.get("answer"),
        }
    )

    try:
        stage = wait_for_stage({"resolution", "done", "tearing_down"}, timeout=300)
        if stage == "resolution":
            logger.info("Resolution stage reached, submitting empty string")
            submit_to_conductor("")
            wait_for_stage({"done", "tearing_down"}, timeout=300)
    except TimeoutError:
        logger.warning("Timed out waiting for done stage")

    write_session(logs_dir, problem_id, stages)
    _finish(logs_dir, problem_id)


def abort_harness_failure(logs_dir: Path, problem_id: str, diagnosis: dict, reason: str) -> None:
    """End the run as infrastructure failure rather than as a CloudThinker score.

    Submits a marker instead of whatever text is in hand: the run must be
    greppable as broken, never averaged in as a low score. Still advances the
    conductor so a suite does not stall 300s per problem waiting on a submission
    that is not coming. Exits 2, so a chain script can tell this apart from both
    a clean run (0) and a crash.
    """
    (logs_dir / "HARNESS_FAILURE.txt").write_text(
        f"{HARNESS_FAILURE_MARKER}\nproblem={problem_id}\nreason={reason}\n"
        f"error={diagnosis.get('error')}\n"
        f"conversation_id={diagnosis.get('conversation_id')}\n"
        f"partial_answer={(diagnosis.get('answer') or '')[:2000]}\n"
    )
    logger.error(f"{HARNESS_FAILURE_MARKER}: {problem_id} — result is INVALID, exclude it. {reason}")
    submit_to_conductor(f"{HARNESS_FAILURE_MARKER}: {reason}")
    _save_stage_result(logs_dir, "diagnosis", diagnosis)
    write_session(
        logs_dir,
        problem_id,
        [
            {
                "name": "diagnosis",
                "conversation_id": diagnosis.get("conversation_id"),
                "submitted": diagnosis.get("answer"),
            }
        ],
    )
    _finish(logs_dir, problem_id)
    sys.exit(2)


def _save_stage_result(logs_dir: Path, stage: str, summary: dict) -> None:
    result_file = logs_dir / f"{stage}_result.json"
    with open(result_file, "w") as f:
        json.dump(
            {
                "stage": stage,
                "conversation_id": summary.get("conversation_id"),
                "status": summary.get("status"),
                "error": summary.get("error"),
                "answer": summary.get("answer"),
                "tool_calls": tool_names(summary),
                "executed_tools": db_tool_calls(summary.get("conversation_id")),
                "approvals": summary.get("approvals", 0),
                "timestamp": datetime.now(UTC).isoformat(),
            },
            f,
            indent=2,
        )
    logger.info(f"Saved {stage} result to {result_file}")


def _finish(logs_dir: Path, problem_id: str) -> None:
    summary = {
        "problem_id": problem_id,
        "driver": "cloudthinker",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    with open(logs_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("CloudThinker driver finished")


if __name__ == "__main__":
    main()
