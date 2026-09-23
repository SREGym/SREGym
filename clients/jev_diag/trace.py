"""Decision trace for the Jev diagnosis agent.

Two artifacts come out of one run:

* ``<prefix>_trace.jsonl``: one JSON record per event, written as it happens
  (thread-safe, flushed per line). This is the analysis format: every kubectl
  call with timing, the per-component signals code derived, budget trims, the
  exact state and questions sent to Jev, every answer with its full
  probability distribution, the ranking code derived from it, the evidence
  list, the assembled text, and the submission result.
* ``trajectory/<ts>_<problem>_jev_diag_agent_trajectory.jsonl``: the same run
  rendered in the Stratus trajectory format that ``visualizer/`` reads and
  ``atif_converter`` converts to ATIF, so a Jev run sits next to LLM-agent
  runs in the existing tooling.

``python -m clients.jev_diag.trace <trace.jsonl>`` prints a timeline summary.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AGENT_NAME = "jev_diag"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class DecisionTrace:
    """Append-only JSONL event log with timing helpers."""

    def __init__(self, path: Path, *, problem_id: str, run_args: dict[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.problem_id = problem_id
        self.started = time.monotonic()
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._file = self.path.open("a", encoding="utf-8")
        self.record("run", "start", problem_id=problem_id, agent=AGENT_NAME, args=run_args or {})

    # ------------------------------------------------------------------ core

    def record(self, phase: str, event: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            rec = {
                "seq": self._seq,
                "ts": _now(),
                "elapsed_ms": int((time.monotonic() - self.started) * 1000),
                "phase": phase,
                "event": event,
                **payload,
            }
            self.records.append(rec)
            self._file.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
            self._file.flush()
            return rec

    @contextmanager
    def timed(self, phase: str, event: str, **payload: Any) -> Iterator[dict[str, Any]]:
        """Record ``<event>`` once on exit with ``duration_ms`` and ``error`` (re-raised)."""
        start = time.monotonic()
        extra: dict[str, Any] = {}
        try:
            yield extra
        except BaseException as exc:
            self.record(
                phase,
                event,
                duration_ms=int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(limit=8),
                **payload,
                **extra,
            )
            raise
        self.record(phase, event, duration_ms=int((time.monotonic() - start) * 1000), **payload, **extra)

    def kubectl_observer(self) -> Callable[[dict[str, Any]], None]:
        """Callback for ``collector.set_kubectl_observer``: one record per kubectl invocation."""

        def observe(call: dict[str, Any]) -> None:
            self.record("collect", "kubectl", **call)

        return observe

    def close(self, status: str, **payload: Any) -> None:
        self.record("run", "end", status=status, total_ms=int((time.monotonic() - self.started) * 1000), **payload)
        with self._lock:
            self._file.close()

    # ------------------------------------------------------------ trajectory

    def write_trajectory(self, out_dir: Path, *, stage: str = "diagnosis") -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%m%d_%H%M")
        path = out_dir / f"{stamp}_{self.problem_id}_{AGENT_NAME}_agent_trajectory.jsonl"
        write_trajectory(self.records, path, problem_id=self.problem_id, stage=stage)
        return path


# --------------------------------------------------------------------------- trajectory rendering


def _tool_msg(content: Any, call_id: str, name: str) -> dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content, default=str, ensure_ascii=False)
    return {"type": "ToolMessage", "content": text, "tool_call_id": call_id, "name": name}


def build_messages(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Render trace records as LangChain-style messages (the Stratus trajectory vocabulary).

    Returns (messages, submitted).
    """
    by_event: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in records:
        by_event.setdefault((rec["phase"], rec["event"]), []).append(rec)

    def first(phase: str, event: str) -> dict[str, Any] | None:
        recs = by_event.get((phase, event))
        return recs[0] if recs else None

    msgs: list[dict[str, Any]] = []
    start = first("run", "start") or {}
    msgs.append(
        {
            "type": "SystemMessage",
            "content": "Jev diagnosis agent: deterministic cluster state collection, then typed System One "
            "questions (TypeSafe Jev) to select the root-cause component. No generative reasoning; the "
            "diagnosis text is assembled in code from typed answers.\n"
            f"run args: {json.dumps(start.get('args', {}), default=str)}",
        }
    )
    app = first("setup", "app_info")
    if app:
        msgs.append({"type": "HumanMessage", "content": json.dumps(app.get("app_info", {}), indent=2, default=str)})

    kubectl_calls = by_event.get(("collect", "kubectl"), [])
    if kubectl_calls:
        tool_calls = [
            {"name": "kubectl", "args": {"cmd": c.get("cmd")}, "id": f"kubectl_{c['seq']}"} for c in kubectl_calls
        ]
        msgs.append(
            {"type": "AIMessage", "content": "Collecting cluster state deterministically.", "tool_calls": tool_calls}
        )
        for c in kubectl_calls:
            outcome = (
                c.get("error")
                or f"exit {c.get('returncode')} in {c.get('duration_ms')} ms, {c.get('stdout_bytes')} bytes"
            )
            msgs.append(_tool_msg(outcome, f"kubectl_{c['seq']}", "kubectl"))

    summary = first("collect", "summary")
    if summary:
        body = {k: v for k, v in summary.items() if k not in ("seq", "ts", "elapsed_ms", "phase", "event")}
        msgs.append(
            {
                "type": "AIMessage",
                "content": "Collection summary (signals computed in code):\n" + json.dumps(body, indent=2, default=str),
            }
        )

    responses = {r["label"]: r for r in by_event.get(("jev", "response"), [])}
    for req in by_event.get(("jev", "request"), []):
        label = req["label"]
        resp = responses.get(label)
        call_id = f"jev_{label}"
        usage = (resp or {}).get("usage") or {}
        ai: dict[str, Any] = {
            "type": "AIMessage",
            "content": f"Jev request `{label}`: {len(req.get('questions', {}))} question(s) over ~{req.get('state_tokens')} state tokens.",
            "tool_calls": [
                {
                    "name": "jev.system_one",
                    "args": {"questions": req.get("questions"), "state": req.get("state")},
                    "id": call_id,
                }
            ],
        }
        if usage.get("input_tokens") is not None or usage.get("output_tokens") is not None:
            ai["usage_metadata"] = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
            }
        if resp:
            ai["response_metadata"] = {"model_name": resp.get("model"), "latency_ms": resp.get("latency_ms")}
        msgs.append(ai)
        if resp:
            msgs.append(_tool_msg(resp.get("error") or resp.get("answers"), call_id, "jev.system_one"))

    for key in (("decide", "component"), ("decide", "evidence"), ("decide", "characterization")):
        rec = first(*key)
        if rec:
            body = {k: v for k, v in rec.items() if k not in ("seq", "ts", "elapsed_ms", "phase", "event")}
            msgs.append(
                {
                    "type": "AIMessage",
                    "content": f"Decision step `{key[1]}` (derived in code):\n"
                    + json.dumps(body, indent=2, default=str),
                }
            )

    text = first("decide", "diagnosis_text")
    submission = first("submit", "response")
    submitted = bool(submission and not submission.get("error"))
    if text:
        final: dict[str, Any] = {"type": "AIMessage", "content": text.get("text", "")}
        if submission is not None or first("submit", "skipped") is None:
            final["tool_calls"] = [
                {"name": "submit", "args": {"ans": text.get("text", ""), "stage": "diagnosis"}, "id": "submit_1"}
            ]
        msgs.append(final)
        if submission is not None:
            msgs.append(_tool_msg(submission.get("error") or submission.get("response"), "submit_1", "submit"))

    error = first("run", "error")
    if error:
        msgs.append({"type": "AIMessage", "content": f"Run failed: {error.get('error')}\n{error.get('traceback', '')}"})
    return msgs, submitted


def build_events(
    messages: list[dict[str, Any]], *, submitted: bool, stage: str, problem_id: str, stamp: str
) -> list[dict[str, Any]]:
    """Cumulative snapshots at each tool-result boundary, matching visualizer/process.py."""
    events: list[dict[str, Any]] = []
    accumulated: list[dict[str, Any]] = []
    steps = 0
    i = 0
    while i < len(messages):
        accumulated.append(messages[i])
        if messages[i]["type"] == "ToolMessage":
            steps += 1
            while i + 1 < len(messages) and messages[i + 1]["type"] == "ToolMessage":
                i += 1
                accumulated.append(messages[i])
                steps += 1
            events.append(_event(accumulated, steps, len(events), False, stage, problem_id, stamp))
        i += 1
    if not events or accumulated != events[-1]["messages"]:
        events.append(_event(accumulated, steps, len(events), submitted, stage, problem_id, stamp))
    else:
        events[-1]["submitted"] = submitted
    return events


def _event(accumulated, steps, index, submitted, stage, problem_id, stamp) -> dict[str, Any]:
    return {
        "type": "event",
        "stage": stage,
        "event_index": index,
        "num_steps": steps,
        "submitted": submitted,
        "rollback_stack": "",
        "messages": list(accumulated),
        "last_message": accumulated[-1] if accumulated else {},
        "problem_id": problem_id,
        "timestamp": stamp,
    }


def write_trajectory(records: list[dict[str, Any]], path: Path, *, problem_id: str, stage: str = "diagnosis") -> Path:
    messages, submitted = build_messages(records)
    now = datetime.now()
    stamp = now.strftime("%m%d_%H%M")
    events = build_events(messages, submitted=submitted, stage=stage, problem_id=problem_id, stamp=stamp)
    with Path(path).open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "metadata",
                    "problem_id": problem_id,
                    "timestamp": stamp,
                    "timestamp_readable": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "total_stages": 1,
                    "total_events": len(events),
                    "agent": AGENT_NAME,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(json.dumps({"type": "stage_start", "stage": stage, "num_events": len(events)}) + "\n")
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    return Path(path)


# --------------------------------------------------------------------------- analysis


def load_trace(path: Path | str) -> list[dict[str, Any]]:
    records = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def summarize_records(records: list[dict[str, Any]]) -> str:
    """Human-readable timeline of one run."""
    out: list[str] = []
    by = {}
    for r in records:
        by.setdefault((r["phase"], r["event"]), []).append(r)

    def one(phase, event):
        recs = by.get((phase, event))
        return recs[0] if recs else None

    start, end = one("run", "start") or {}, one("run", "end") or {}
    out.append(
        f"problem: {start.get('problem_id')}   status: {end.get('status', 'incomplete')}   total: {end.get('total_ms', '?')} ms"
    )

    kubectl = by.get(("collect", "kubectl"), [])
    if kubectl:
        failed = [c for c in kubectl if c.get("error")]
        total_ms = sum(c.get("duration_ms") or 0 for c in kubectl)
        out.append(f"kubectl: {len(kubectl)} calls, {len(failed)} failed, {total_ms} ms wall across calls")
        slow = sorted(kubectl, key=lambda c: -(c.get("duration_ms") or 0))[:3]
        for c in slow:
            out.append(f"  slowest: {c.get('duration_ms')} ms  {c.get('cmd')}")
    coll = one("collect", "summary")
    if coll:
        comps = coll.get("components", {})
        unhealthy = {k: v for k, v in comps.items() if v.get("signals")}
        log_only = [k for k, v in comps.items() if not v.get("signals") and v.get("log_error_lines")]
        out.append(
            f"components: {len(comps)} collected, {len(unhealthy)} with structural signals, "
            f"{len(log_only)} with only log errors, errors: {len(coll.get('collection_errors', []))}"
        )
        for cid, c in unhealthy.items():
            extra = f" (+{c['log_error_lines']} log error lines)" if c.get("log_error_lines") else ""
            out.append(f"  {cid}: {len(c['signals'])} signal(s){extra}; first: {c['signals'][0]}")

    for fit in by.get(("jev", "budget_fit"), []):
        out.append(
            f"budget[{fit['label']}]: {fit['tokens_before']} -> {fit['tokens_after']} est. tokens, trims: {fit['applied'] or 'none'}"
        )
    for req in by.get(("jev", "request"), []):
        resp = next((r for r in by.get(("jev", "response"), []) if r["label"] == req["label"]), {})
        usage = resp.get("usage") or {}
        out.append(
            f"jev[{req['label']}]: {list(req.get('questions', {}))} model={resp.get('model')} "
            f"latency={resp.get('latency_ms')} ms tokens in/out={usage.get('input_tokens')}/{usage.get('output_tokens')}"
            + (f" ERROR {resp.get('error')}" if resp.get("error") else "")
        )
    dec = one("decide", "component")
    if dec:
        out.append(
            f"component: {dec.get('chosen')} (confidence {dec.get('confidence')}, fault_visible {dec.get('fault_visible')})"
        )
        for cid, p in dec.get("ranked", [])[:5]:
            out.append(f"  {p:.3f}  {cid}")
        if dec.get("low_confidence"):
            out.append("  flagged low confidence")
    for st in by.get(("decide", "step"), []):
        if st.get("node") == "cluster":
            out.append(f"step {st.get('step')}: cluster objects -> {st.get('result')}")
        else:
            out.append(
                f"step {st.get('step')}: investigate {st.get('component')} -> {st.get('verdict')} "
                f"(origin {st.get('origin_p')}, victim {st.get('victim_p')}), next={st.get('next')} ({st.get('next_p')}), "
                f"category={st.get('category')}"
            )
            if st.get("key_evidence"):
                out.append(f"        key evidence: {str(st['key_evidence'])[:160]}")
    ch = one("decide", "characterization")
    if ch:
        out.append(f"category: {ch.get('category')} (confidence {ch.get('category_confidence')})")
        for cat, p in ch.get("category_ranked", [])[:3]:
            out.append(f"  {p:.3f}  {cat}")
        if ch.get("key_evidence_text"):
            out.append(
                f"key evidence [{ch.get('key_evidence_id')}] p={ch.get('key_evidence_probability')}: {ch.get('key_evidence_text')}"
            )
    sub = one("submit", "response")
    if sub:
        out.append(f"submission: {'error ' + str(sub['error']) if sub.get('error') else sub.get('response')}")
    err = one("run", "error")
    if err:
        out.append(f"error: {err.get('error')}")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a jev_diag decision trace")
    parser.add_argument("trace", help="path to *_trace.jsonl")
    parser.add_argument("--trajectory", metavar="DIR", help="also render a visualizer trajectory into DIR")
    args = parser.parse_args()
    records = load_trace(args.trace)
    print(summarize_records(records))
    if args.trajectory:
        problem_id = next((r.get("problem_id") for r in records if r.get("event") == "start"), "unknown")
        path = (
            Path(args.trajectory)
            / f"{datetime.now().strftime('%m%d_%H%M')}_{problem_id}_{AGENT_NAME}_agent_trajectory.jsonl"
        )
        write_trajectory(records, path, problem_id=problem_id)
        print(f"trajectory written to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
