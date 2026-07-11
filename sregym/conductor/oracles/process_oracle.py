"""
ProcessOracle: spec-driven evidence evaluation for agent tool-call traces.

An agent passes if it:
  1. Completed at least one full investigation path defined in the spec (known paths), OR
  2. Collected sufficient evidence via a novel path — evaluated by LLM fallback.

AND it has at least one corroboration check satisfied.

The spec YAML is the artifact contributed alongside each new fault.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TraceEvent:
    tool_name: str
    args: dict
    result: str = ""

    @property
    def command(self) -> str:
        return self.args.get("command", self.args.get("cmd", ""))


@dataclass
class PathResult:
    path_id: str
    description: str
    matched: bool
    signals_matched: list[str] = field(default_factory=list)
    signals_missed: list[str] = field(default_factory=list)


@dataclass
class ProcessOracleResult:
    passed: bool
    score: float
    path_taken: str | None
    corroboration_met: bool
    path_results: list[PathResult]
    novel_path_used: bool
    novel_path_sufficient: bool | None
    report: str
    tool_call_count: int
    unique_command_count: int
    anchoring_risk: bool


# ---------------------------------------------------------------------------
# Spec loader
# ---------------------------------------------------------------------------


def load_spec(spec_path: Path | str) -> dict:
    with Path(spec_path).open() as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Signal matching helpers
# ---------------------------------------------------------------------------


def _matches(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def _check_signal(signal: dict, trace: list[TraceEvent], within_event: TraceEvent | None = None) -> bool:
    """
    Return True if the signal is satisfied.

    signal keys:
      field: "command" | "result" | "any"
      pattern: regex
      numeric_nonzero: bool  (result must contain "<pattern> <N>" where N > 0)
      across_calls: bool     (pattern can appear in any call, not just within_event)
    """
    pattern = signal["pattern"]
    field_name = signal.get("field", "any")
    numeric = signal.get("numeric_nonzero", False)
    across_calls = signal.get("across_calls", False)

    candidates = trace if across_calls else ([within_event] if within_event else trace)

    for te in candidates:
        if te is None:
            continue
        if field_name == "command":
            text = te.command
        elif field_name == "result":
            text = te.result
        else:
            text = te.command + " " + te.result

        if not _matches(text, pattern):
            continue

        if numeric:
            m = re.search(rf"{pattern}\s+(\d+)", text, re.IGNORECASE)
            if not (m and int(m.group(1)) > 0):
                continue

        return True
    return False


# ---------------------------------------------------------------------------
# Path / corroboration evaluation
# ---------------------------------------------------------------------------


def _evaluate_path(path_spec: dict, trace: list[TraceEvent]) -> PathResult:
    signals = path_spec.get("signals", [])
    matched = []
    missed = []

    for sig in signals:
        across = sig.get("across_calls", False)
        ok = _check_signal(sig, trace) if across else any(_check_signal(sig, trace, te) for te in trace)
        (matched if ok else missed).append(sig["pattern"])

    return PathResult(
        path_id=path_spec["id"],
        description=path_spec["description"],
        matched=len(missed) == 0 and len(matched) > 0,
        signals_matched=matched,
        signals_missed=missed,
    )


def _evaluate_corroboration(corroboration_specs: list[dict], trace: list[TraceEvent]) -> bool:
    for cor in corroboration_specs:
        result = _evaluate_path(cor, trace)
        if result.matched:
            return True
    return False


# ---------------------------------------------------------------------------
# Novel path LLM fallback
# ---------------------------------------------------------------------------


def _has_any_evidence(trace: list[TraceEvent], keywords: list[str]) -> bool:
    for te in trace:
        text = (te.command + " " + te.result).lower()
        if any(k.lower() in text for k in keywords):
            return True
    return False


def _detect_anchoring_risk(anchoring_traps: list[dict], trace: list[TraceEvent]) -> bool:
    """
    Return True if the agent triggered a misleading signal (anchoring trap)
    and never followed up with a deeper investigation (escape signal).
    """
    for trap in anchoring_traps:
        misleading = trap.get("misleading_signal", {})
        if not _check_signal(misleading, trace):
            continue
        for escape_sig in trap.get("escape_signals", []):
            if _check_signal(escape_sig, trace):
                break
        else:
            return True
    return False


def _llm_novel_path_analysis(trace: list[TraceEvent], spec: dict) -> bool:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from llm_backend.init_backend import get_llm_backend_for_judge
    except ImportError:
        return False

    backend = get_llm_backend_for_judge()
    if backend is None:
        return False

    prompt = spec.get("novel_path_prompt", "")
    if not prompt:
        return False

    trace_text = "\n".join(f"[{te.tool_name}] CMD: {te.command[:200]}\nRESULT: {te.result[:400]}" for te in trace)

    messages = [
        SystemMessage(content="You are evaluating SRE agent investigation quality."),
        HumanMessage(content=f"{prompt}\n\n--- TOOL CALL TRACE ---\n{trace_text}"),
    ]

    try:
        resp = backend.inference(messages)
        clean = re.sub(r"```(?:json)?\s*|\s*```", "", resp.content.strip())
        data = json.loads(clean)
        return bool(data.get("sufficient", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core oracle
# ---------------------------------------------------------------------------


class ProcessOracle:
    def __init__(self, spec: dict | None = None, spec_path: Path | str | None = None):
        if spec is not None:
            self._spec = spec
        elif spec_path is not None:
            self._spec = load_spec(spec_path)
        else:
            raise ValueError("Either spec or spec_path must be provided")

    @classmethod
    def for_problem(cls, problem_id: str) -> ProcessOracle:
        specs_dir = Path(__file__).parent / "llm_as_a_judge" / "process_specs"
        path = specs_dir / f"{problem_id}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"No process spec found for {problem_id} at {path}")
        return cls(spec_path=path)

    def evaluate(self, trajectory_path: Path | str) -> ProcessOracleResult:
        trace = self._extract_trace(self._load_jsonl(Path(trajectory_path)))

        known_paths = self._spec.get("investigation_paths", [])
        path_results = [_evaluate_path(p, trace) for p in known_paths]
        matched_path = next((r for r in path_results if r.matched), None)

        corroboration_met = _evaluate_corroboration(self._spec.get("corroboration", []), trace)

        novel_path_used = False
        novel_path_sufficient = None

        evidence_keywords = self._spec.get("evidence_keywords", [])
        if matched_path is None and _has_any_evidence(trace, evidence_keywords):
            novel_path_used = True
            novel_path_sufficient = _llm_novel_path_analysis(trace, self._spec)

        known_path_ok = matched_path is not None
        novel_ok = novel_path_used and novel_path_sufficient is True
        evidence_ok = known_path_ok or novel_ok

        passed = evidence_ok and corroboration_met
        score = round(
            (0.7 if evidence_ok else 0.0) + (0.3 if corroboration_met else 0.0),
            2,
        )

        unique_command_count = len({te.command for te in trace if te.command})
        anchoring_risk = _detect_anchoring_risk(self._spec.get("anchoring_traps", []), trace)

        return ProcessOracleResult(
            passed=passed,
            score=score,
            path_taken=matched_path.path_id if matched_path else ("novel" if novel_ok else None),
            corroboration_met=corroboration_met,
            path_results=path_results,
            novel_path_used=novel_path_used,
            novel_path_sufficient=novel_path_sufficient,
            report=self._build_report(
                passed,
                score,
                matched_path,
                path_results,
                corroboration_met,
                novel_path_used,
                novel_path_sufficient,
                trace,
                anchoring_risk,
            ),
            tool_call_count=len(trace),
            unique_command_count=unique_command_count,
            anchoring_risk=anchoring_risk,
        )

    # ------------------------------------------------------------------
    # Trace extraction (handles both claudecode Bash and stratus MCP formats)
    # ------------------------------------------------------------------

    def _load_jsonl(self, path: Path) -> list[dict]:
        events = []
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def _extract_trace(self, raw_events: list[dict]) -> list[TraceEvent]:
        final: dict[str, dict] = {}
        for ev in raw_events:
            if ev.get("type") != "event":
                continue
            stage = ev.get("stage", "")
            if stage not in final or ev.get("event_index", -1) > final[stage].get("event_index", -1):
                final[stage] = ev

        trace: list[TraceEvent] = []
        pending: dict[str, TraceEvent] = {}

        for ev in final.values():
            for msg in ev.get("messages", []):
                role = msg.get("role", "")
                if role == "assistant":
                    for tc in msg.get("tool_calls", []):
                        te = TraceEvent(tool_name=tc.get("name", ""), args=tc.get("args", {}))
                        tid = tc.get("id", "")
                        if tid:
                            pending[tid] = te
                        else:
                            trace.append(te)
                elif role == "tool":
                    tid = msg.get("tool_use_id", "")
                    result = msg.get("content", "")
                    if tid in pending:
                        pending[tid].result = result
                        trace.append(pending.pop(tid))
                    else:
                        trace.append(TraceEvent(tool_name="unknown", args={}, result=result))

        trace.extend(pending.values())
        return trace

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _build_report(
        self,
        passed,
        score,
        matched_path,
        path_results,
        corroboration_met,
        novel_path_used,
        novel_path_sufficient,
        trace,
        anchoring_risk,
    ) -> str:
        unique_cmds = len({te.command for te in trace if te.command})
        lines = [
            f"ProcessOracle: {'PASS' if passed else 'FAIL'} (score={score:.2f})",
            f"Tool calls: {len(trace)}  unique commands: {unique_cmds}",
            f"Anchoring risk: {'YES — saw misleading signal, no deeper follow-up' if anchoring_risk else 'no'}",
            "",
            "Investigation paths:",
        ]
        for pr in path_results:
            tag = "MATCH" if pr.matched else "miss"
            lines.append(f"  [{tag}] {pr.path_id}: {pr.description}")
            if pr.signals_missed:
                lines.append(f"         missing signals: {pr.signals_missed}")

        if novel_path_used:
            tag = "PASS" if novel_path_sufficient else "FAIL"
            lines.append(f"  [novel path → LLM: {tag}]")

        lines += [
            "",
            f"Corroboration: {'met' if corroboration_met else 'NOT MET'}",
        ]

        if matched_path:
            lines.append(f"Path taken: {matched_path.path_id}")
        elif novel_path_sufficient:
            lines.append("Path taken: novel (LLM-validated)")
        else:
            lines.append("Path taken: none — insufficient evidence")

        return "\n".join(lines)
