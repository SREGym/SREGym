"""
mini-sre agent driver for SREGym.
Entry point for running the mini-sre agent on SREGym tasks.
"""

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

# Add SREGym root to path
sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

from clients.harness.problem_id import resolve_problem_id  # noqa: E402
from clients.harness.token_usage import aggregate_usage  # noqa: E402
from clients.mini_sre import mini  # noqa: E402
from clients.mini_sre.backends import ApiBackend, Reply  # noqa: E402
from clients.mini_sre.tools import CommandResult, run_command  # noqa: E402,F401  (re-exported for tests)

logger = logging.getLogger("all.mini_sre.driver")

# Settings (module level so tests can patch them)

API_HOSTNAME = os.getenv("API_HOSTNAME", "localhost")
API_PORT = os.getenv("API_PORT", "8000")
CONDUCTOR_URL = f"http://{API_HOSTNAME}:{API_PORT}"

AGENT_LOGS_DIR = os.environ.get("AGENT_LOGS_DIR", "./logs/mini-sre")
MODEL = os.environ.get("AGENT_MODEL_ID", "")
REASONING_EFFORT = os.environ.get("AGENT_REASONING_EFFORT")

HARD_CAP = int(os.environ.get("MINI_SRE_HARD_CAP", "80"))
# Replies allowed after the limit message.
WRAP_UP_CALLS = int(os.environ.get("MINI_SRE_WRAP_UP_CALLS", "3"))
COMMAND_TIMEOUT = int(os.environ.get("MINI_SRE_COMMAND_TIMEOUT", "60"))
# SREGym stops the agent this long after it starts (--agent-timeout), for both stages together. The conductor's
# /status says what is left of it; this is used only when it does not.
ATTEMPT_S = float(os.environ.get("MINI_SRE_ATTEMPT_S", "1800"))
# Diagnosis may use up to this share of that time. Mitigation has the rest, and more if diagnosis ends sooner.
DIAGNOSIS_SHARE = float(os.environ.get("MINI_SRE_DIAGNOSIS_SHARE", str(2 / 3)))
RETRY_WAIT_S = float(os.environ.get("MINI_SRE_RETRY_WAIT_S", "30"))
SESSION_REASONING = os.environ.get("MINI_SRE_SESSION_REASONING", "1") != "0"

STORED_STREAM_CHARS = 256 * 1024
SUBMIT_ENDPOINT = re.compile(r"/submit(?:_mcp)?\b")

EXIT_OK = 0
EXIT_INFRA = 1
EXIT_MODEL_FAILURE = 2
EXIT_NO_SUBMISSION = 4  # the stage ended without a submission (limits, repeated format errors)


def get_app_info(max_retries: int = 6, backoff: int = 5) -> dict:
    """Fetch application info from the conductor."""
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


def conductor_status() -> dict | None:
    """Return the conductor's /status answer, or None if the conductor does not answer."""
    try:
        resp = requests.get(f"{CONDUCTOR_URL}/status", timeout=10)
        resp.raise_for_status()
        status = resp.json()
        return status if isinstance(status, dict) else None
    except Exception as e:
        logger.debug(f"Status poll error: {e}")
        return None


def current_stage() -> str | None:
    """Return the conductor's current stage, or None if the conductor does not answer."""
    status = conductor_status()
    return status.get("stage") if status else None


def run_deadline(status: dict | None) -> float | None:
    """Return when SREGym stops the agent, on this process's monotonic clock, or None if /status does not say.

    SREGym gives the agent one time limit for the whole attempt (1800 s by default), diagnosis and mitigation
    together, and reports what is left of it as ``agent_remaining_seconds``.
    """
    left = (status or {}).get("agent_remaining_seconds")
    if isinstance(left, bool) or not isinstance(left, (int, float)):
        return None
    return time.monotonic() + float(left)


def diagnosis_deadline(run_ends: float, agent_started: float) -> float:
    """Return when diagnosis must end: after DIAGNOSIS_SHARE of the attempt's time (1200 of 1800 seconds).

    Over 59 earlier runs of the agent, this left every mitigation that passed enough time, and cut short one
    diagnosis that passed (1450 seconds, in a run whose limit had been raised to 3600).
    """
    return run_ends - (1 - DIAGNOSIS_SHARE) * (run_ends - agent_started)


def wait_for_stage(target_stages: set[str], timeout: int = 300) -> str:
    """Poll the conductor until its stage is in ``target_stages``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stage = current_stage()
        if stage in target_stages:
            logger.info(f"Conductor reached stage: {stage}")
            return stage
        time.sleep(2)
    raise TimeoutError(f"Conductor did not reach {target_stages} within {timeout}s")


def is_external_submit(command: str) -> bool:
    """Check whether a command calls the submission endpoint."""
    return bool(SUBMIT_ENDPOINT.search(command))


class Transcript:
    """Append-only JSONL log of the run."""

    def __init__(self, path: Path):
        """Open the transcript file for appending."""
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, record: dict) -> None:
        """Append one record with a timestamp."""
        record = {"ts": datetime.now().astimezone().isoformat(), **record}
        self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        """Close the transcript file."""
        self._handle.close()


def _stream_record(text: str) -> dict:
    """Return a stream's length, hash and text for the transcript, cut to STORED_STREAM_CHARS."""
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        "text": text[:STORED_STREAM_CHARS],
        "stored_truncated": len(text) > STORED_STREAM_CHARS,
    }


@dataclass
class Session:
    """The conversation, sent in full on every step."""

    pass_reasoning: bool = True
    messages: list[dict] = field(default_factory=list)

    def open(self, system: dict, first_turn: str) -> None:
        """Start the conversation with the system prompt and the first user message."""
        self.messages = [system, {"role": "user", "content": first_turn}]

    def add_user(self, text: str) -> None:
        """Append a user message."""
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, reply: Reply) -> None:
        """Append the model's reply."""
        self.messages.append(reply.as_message(pass_reasoning=self.pass_reasoning))


@dataclass
class StageOutcome:
    """How one stage ended, with its counts and token usage."""

    stage: str
    termination_reason: str
    commands_used: int
    model_calls: int
    usage_records: list[dict] = field(default_factory=list)
    wall_seconds: float = 0.0
    records: int = 0  # command records written by this stage (the next stage numbers its records after them)

    def summary(self) -> dict:
        """Return the stage's numbers for the results file."""
        return {
            "commands_used": self.commands_used,
            "model_calls": self.model_calls,
            "termination_reason": self.termination_reason,
            "wall_seconds": self.wall_seconds,
            "usage_metrics": aggregate_usage(self.usage_records),
        }


def make_backend(model: str, reasoning_effort: str | None) -> ApiBackend:
    """Create the model backend."""
    return ApiBackend(model, reasoning_effort)


def run_stage(
    backend: ApiBackend,
    session: Session,
    transcript: Transcript,
    steps_dir: Path,
    *,
    stage: str,
    work_dir: str,
    run_ends: float,
    stage_ends: float | None = None,
    call_offset: int = 0,
    index_offset: int = 0,
) -> StageOutcome:
    """Run one stage: one bash command per reply until the model submits or a limit is hit.

    The stage ends when the conductor accepts the model's submission, or when the conductor moves to
    another stage after a command. At a limit the model is told once to submit and gets WRAP_UP_CALLS
    more replies. If it does not submit, the stage ends without a submission.

    The stage has until ``stage_ends`` (the end of diagnosis's share), or else until ``run_ends``, when SREGym stops
    the agent. ``run_ends`` is read again from the conductor after every command.
    """
    started = time.monotonic()
    usage_records: list[dict] = []
    model_calls = 0
    used = 0
    consecutive_failures = 0
    format_errors = 0
    wrap_up_sent = False
    wrap_up_calls = 0
    records = 0

    def outcome(reason: str) -> StageOutcome:
        """Build the stage result with the counts so far."""
        return StageOutcome(
            stage,
            reason,
            used,
            model_calls,
            usage_records,
            round(time.monotonic() - started, 3),
            records=records,
        )

    def seconds_left() -> float:
        """Return the seconds left in this stage."""
        ends = run_ends if stage_ends is None else min(stage_ends, run_ends)
        return ends - time.monotonic()

    def with_budget(text: str) -> str:
        """Add the budget line to an observation."""
        return text + mini.budget_notice(used=used, cap=HARD_CAP, seconds_left=seconds_left())

    while True:
        limit = None
        if seconds_left() <= 0:
            limit = ("deadline", "the time limit")
        elif used >= HARD_CAP:
            limit = ("hard_cap", f"the limit of {HARD_CAP} commands")
        if limit is not None:
            if not wrap_up_sent:
                wrap_up_sent = True
                logger.info(f"[{stage}] {limit[0]} reached; asking for a submission")
                transcript.write({"type": "wrap_up", "stage": stage, "reason": limit[0], "commands": used})
                session.add_user(mini.wrap_up_text(limit[1]))
            if wrap_up_calls >= WRAP_UP_CALLS:
                return outcome(f"{limit[0]}_no_submission")
            wrap_up_calls += 1

        model_calls += 1
        call_number = call_offset + model_calls
        reply = backend.complete(session.messages, step_dir=steps_dir / f"step_{call_number:02d}")
        usage_records.append(reply.usage)
        action, n_actions = (None, 0) if reply.error else mini.parse_action(reply.content)
        transcript.write(
            {
                "type": "model_call",
                "stage": stage,
                "call": call_number,
                "messages": len(session.messages),
                "latency_s": reply.latency_s,
                "usage": reply.usage,
                "finish_reason": reply.finish_reason,
                "content": (reply.content or "")[:20000],
                "content_from_reasoning": reply.content_from_reasoning,
                "action": action,
                "n_actions": n_actions,
                "error": reply.error,
            }
        )
        if reply.error:
            consecutive_failures += 1
            logger.warning(f"Model call {call_number} unusable: {reply.error}")
            if consecutive_failures >= 2:
                return outcome("model_failure")
            time.sleep(RETRY_WAIT_S)
            continue
        consecutive_failures = 0
        session.add_assistant(reply)

        if action is None:
            format_errors += 1
            logger.info(f"[{stage}] call {call_number}: format error ({n_actions} actions)")
            if format_errors >= mini.MAX_CONSECUTIVE_FORMAT_ERRORS:
                return outcome("repeated_format_error")
            session.add_user(mini.format_error_text(n_actions))
            continue
        format_errors = 0
        records += 1
        index = index_offset + records

        logger.info(f"[{stage}] call {call_number}: {action[:160]}")
        result = run_command(action, COMMAND_TIMEOUT, cwd=work_dir)
        separator = "\n" if result.stdout and not result.stdout.endswith("\n") else ""
        output = result.stdout + (separator + result.stderr if result.stderr else "")
        transcript.write(
            {
                "type": "command",
                "stage": stage,
                "index": index,
                "command": action,
                "exit_code": result.exit_code,
                "duration_s": result.duration_s,
                "timed_out": result.timed_out,
                "stdout": _stream_record(result.stdout),
                "stderr": _stream_record(result.stderr),
            }
        )
        used += 1
        # one /status per command: the time left for the budget line, and the stage for the check below
        status = conductor_status()
        run_ends = run_deadline(status) or run_ends
        if result.timed_out:
            session.add_user(with_budget(mini.timeout_text(action, output)))
            continue
        session.add_user(with_budget(mini.observation_text(result.exit_code, output)))
        if is_external_submit(action) and mini.accepted_submission(output, stage):
            # The conductor accepted the submission, so the stage ends here.
            logger.info(f"[{stage}] the conductor accepted the model's submission; ending the stage")
            return outcome("submitted_by_command")
        observed = status.get("stage") if status else None
        if observed not in {stage, None}:
            logger.info(f"[{stage}] the model submitted with a command; conductor stage is now {observed}")
            return outcome("submitted_by_command")


def save_results(path: Path, problem_id: str, return_code: int, usage: dict, meta: dict) -> None:
    """Write the results file for the run."""
    payload = {
        "problem_id": problem_id,
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "return_code": return_code,
        "success": return_code == EXIT_OK,
        "usage_metrics": usage,
        "mini_sre": meta,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info(f"Saved results to {path}")


def run_preflight() -> None:
    """Check the model settings with one small call."""
    if not MODEL:
        print("AGENT_MODEL_ID is not set")
        sys.exit(1)
    backend = make_backend(MODEL, REASONING_EFFORT)
    with tempfile.TemporaryDirectory(prefix="mini-sre-preflight-") as tmp:
        reply = backend.complete(
            [backend.system_message("Reply with the single word ok."), {"role": "user", "content": "ok?"}],
            step_dir=Path(tmp) / "preflight",
        )
        if reply.error:
            print(reply.error)
            sys.exit(1)
    sys.exit(0)


def main():
    """Run the mini-sre agent on the current problem."""
    parser = argparse.ArgumentParser(description="Run the SREGym mini-sre agent")
    parser.add_argument("--logs-dir", default=AGENT_LOGS_DIR)
    parser.add_argument("--problem-id", default=None, help="artifact id (default: SREGYM_ARTIFACT_ID)")
    args = parser.parse_args()
    agent_started = time.monotonic()  # SREGym starts the clock of its limit when it starts the agent

    if not MODEL:
        logger.error("AGENT_MODEL_ID is not set (SREGym sets it from --model)")
        sys.exit(EXIT_INFRA)
    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    problem_id = resolve_problem_id(cli_problem_id=args.problem_id)

    transcript = Transcript(logs_dir / "mini_sre_transcript.jsonl")
    results_path = logs_dir / f"mini_sre_results_{problem_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backend = make_backend(MODEL, REASONING_EFFORT)
    try:
        backend_version = backend.version()
    except Exception as e:
        backend_version = f"unavailable: {e}"
    mini_sre_meta: dict = {
        "backend": backend.name,
        "backend_version": backend_version,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        # The ATIF adapter uses these fields to recognize a mini-sre transcript.
        "protocol": "mini",
        "submit_mode": "curl",
        "submission_mode": "full",
        "context": "session",
        "session_reasoning": SESSION_REASONING,
        "hard_cap": HARD_CAP,
        "attempt_s": ATTEMPT_S,
        "diagnosis_share": round(DIAGNOSIS_SHARE, 3),
        "command_timeout_s": COMMAND_TIMEOUT,
        "artifact_id": problem_id,
        "stages": {},
    }
    transcript.write({"type": "meta", **mini_sre_meta, "conductor_url": CONDUCTOR_URL})
    logger.info("=" * 60)
    logger.info(f"mini-sre driver starting: {mini_sre_meta}")
    logger.info("=" * 60)

    try:
        stage = wait_for_stage({"diagnosis", "mitigation"}, timeout=300)
    except TimeoutError:
        logger.error("Timed out waiting for the conductor to reach an agent stage")
        sys.exit(EXIT_INFRA)
    try:
        app_info = get_app_info()
    except Exception as e:
        logger.error(f"Failed to get app info: {e}")
        sys.exit(EXIT_INFRA)

    session = Session(pass_reasoning=SESSION_REASONING)
    # The task text covers both stages, so no message is sent when mitigation starts.
    session.open(backend.system_message(mini.system_text()), mini.instance_text(app_info))
    transcript.write({"type": "prompt", "messages": [dict(m) for m in session.messages]})
    # SREGym's limit for the whole attempt, as the conductor reports it; diagnosis may use its share of it
    run_ends = run_deadline(conductor_status()) or agent_started + ATTEMPT_S
    diagnosis_ends = diagnosis_deadline(run_ends, agent_started)
    mini_sre_meta["attempt_s"] = round(run_ends - agent_started)

    def run(stage_name: str, **kwargs) -> StageOutcome:
        """Run one stage with the shared session and transcript."""
        return run_stage(
            backend,
            session,
            transcript,
            logs_dir / "steps",
            stage=stage_name,
            work_dir=str(logs_dir),
            **kwargs,
        )

    return_code = EXIT_OK
    usage_records: list[dict] = []
    submitted_stages: list[str] = []

    def finish_snapshot() -> None:
        """Write the results file with the stages submitted so far."""
        mini_sre_meta["submitted_stages"] = list(submitted_stages)
        save_results(results_path, problem_id, return_code, aggregate_usage(usage_records), mini_sre_meta)

    def record(outcome: StageOutcome) -> None:
        """Save a finished stage. A stage without a submission ends the run."""
        nonlocal return_code
        usage_records.extend(outcome.usage_records)
        mini_sre_meta["stages"][outcome.stage] = outcome.summary()
        if outcome.termination_reason == "submitted_by_command":
            submitted_stages.append(outcome.stage)
            transcript.write({"type": "submit", "stage": outcome.stage, "by": "command"})
        else:
            logger.warning(f"{outcome.stage} ended without a submission ({outcome.termination_reason}); exiting")
            if return_code == EXIT_OK:
                return_code = (
                    EXIT_MODEL_FAILURE if outcome.termination_reason == "model_failure" else EXIT_NO_SUBMISSION
                )
        transcript.write({"type": "end", "stage": outcome.stage, "reason": outcome.termination_reason})
        finish_snapshot()

    calls = 0
    records = 0
    if stage == "diagnosis":
        outcome = run("diagnosis", run_ends=run_ends, stage_ends=diagnosis_ends)
        calls, records = outcome.model_calls, outcome.records
        # The diagnosis numbers also go at the top level of the results file.
        mini_sre_meta.update(
            commands_used=outcome.commands_used,
            model_calls=outcome.model_calls,
            termination_reason=outcome.termination_reason,
            wall_seconds=outcome.wall_seconds,
        )
        record(outcome)
        if "diagnosis" not in submitted_stages:
            stage = None
        else:
            try:
                stage = wait_for_stage({"mitigation", "tearing_down", "done"}, timeout=600)
            except TimeoutError:
                logger.warning("Timed out waiting for the stage after diagnosis")
                stage = None
    else:
        logger.info("Benchmark starts at mitigation; skipping diagnosis")

    if stage == "mitigation":
        run_ends = run_deadline(conductor_status()) or run_ends
        record(run("mitigation", run_ends=run_ends, call_offset=calls, index_offset=records))
        with contextlib.suppress(TimeoutError):
            wait_for_stage({"tearing_down", "done"}, timeout=600)

    finish_snapshot()
    transcript.write({"type": "end", "stage": "run", "return_code": return_code})
    transcript.close()
    logger.info(f"mini-sre driver finished with return code {return_code}")
    sys.exit(return_code)


if __name__ == "__main__":
    main()
