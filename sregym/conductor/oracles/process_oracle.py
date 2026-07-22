"""
ProcessOracle: spec-driven causal-chain evaluation for agent tool-call traces.

An agent passes when it demonstrates evidence of both:
  1. The observable fault symptom (first node in the causal chain), and
  2. The root cause at the injection point (last node in the causal chain).

Score reflects coverage of the full causal chain between symptom and root cause,
mirroring the Node F1 / Edge F1 metrics from process-level RCA evaluation.

Reasoning failure flags surface specific investigation shortfalls:
  RF-08  evidential insufficiency  symptom reached, root cause never touched
  RF-05  spurious attribution      root cause reached, symptom never demonstrated
  RF-13  anchoring bias            misleading signal seen, no escape to correct path

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
class ChainNodeResult:
    node_id: str
    component: str
    touched: bool
    signals_matched: list[str] = field(default_factory=list)
    signals_missed: list[str] = field(default_factory=list)
    signals_leaked: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass
class ProcessOracleResult:
    passed: bool
    score: float

    symptom_reached: bool
    root_cause_reached: bool
    node_f1: float
    edge_f1: float
    chain_results: list[ChainNodeResult]

    rf08_evidential_insufficiency: bool
    rf05_spurious_attribution: bool
    rf13_anchoring_risk: bool

    report: str
    tool_call_count: int
    unique_command_count: int
    baseline_run_count: int = 0
    unverifiable_nodes: list[str] = field(default_factory=list)


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


def _check_signal(
    signal: dict,
    trace: list[TraceEvent],
    within_event: TraceEvent | None = None,
) -> bool:
    """
    Return True if the signal is satisfied anywhere in the trace (or within
    a single event when within_event is supplied).

    signal keys:
      field:           "command" | "result" | "any"
      pattern:         regex
      numeric_nonzero: bool  (result must contain "<pattern> <N>" where N > 0)
      across_calls:    bool  (pattern may appear in any call, ignores within_event)
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


def _signal_fires(signal: dict, trace: list[TraceEvent]) -> bool:
    if signal.get("across_calls", False):
        return _check_signal(signal, trace)
    return any(_check_signal(signal, trace, te) for te in trace)


def _node_unverifiable(result: ChainNodeResult) -> bool:
    """
    A node is unverifiable when the baseline gate rejected every signal it had,
    leaving nothing that can separate the faulty trace from a healthy one. This
    is a spec defect, not an agent failure: the node's touched=False carries no
    information about the agent, so agent-facing reasoning-failure flags for it
    are suppressed.
    """
    return bool(result.signals_leaked) and not result.signals_matched and not result.signals_missed


# ---------------------------------------------------------------------------
# Chain node evaluation
# ---------------------------------------------------------------------------


def _evaluate_chain_node(
    node_spec: dict,
    trace: list[TraceEvent],
    baseline_runs: list[list[TraceEvent]] | None = None,
) -> ChainNodeResult:
    """
    Evaluate one causal-chain node against the agent trace.

    When a baseline corpus of healthy runs is supplied, any signal that also
    fires on the baseline is non-discriminative: it cannot separate the faulty
    trace from a normal one, so it is rejected and excluded from the node's
    coverage requirement. A node is credited only when it retains at least one
    discriminative signal and every surviving signal is matched.
    """
    baseline_runs = baseline_runs or []
    signals = node_spec.get("signals", [])
    matched: list[str] = []
    missed: list[str] = []
    leaked: list[tuple[str, int, int]] = []

    for sig in signals:
        pattern = sig["pattern"]
        leak_count = sum(1 for run in baseline_runs if _signal_fires(sig, run))
        if leak_count > 0:
            leaked.append((pattern, leak_count, len(baseline_runs)))
            continue
        (matched if _signal_fires(sig, trace) else missed).append(pattern)

    return ChainNodeResult(
        node_id=node_spec["id"],
        component=node_spec.get("component", ""),
        touched=len(missed) == 0 and len(matched) > 0,
        signals_matched=matched,
        signals_missed=missed,
        signals_leaked=leaked,
    )


# ---------------------------------------------------------------------------
# Anchoring risk detection (RF-13)
# ---------------------------------------------------------------------------


def _detect_anchoring_risk(anchoring_traps: list[dict], trace: list[TraceEvent]) -> bool:
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


# ---------------------------------------------------------------------------
# Core oracle
# ---------------------------------------------------------------------------


class ProcessOracle:
    def __init__(
        self,
        spec: dict | None = None,
        spec_path: Path | str | None = None,
        baseline_paths: list[Path | str] | None = None,
    ) -> None:
        if spec is not None:
            self._spec = spec
        elif spec_path is not None:
            self._spec = load_spec(spec_path)
        else:
            raise ValueError("Either spec or spec_path must be provided")

        self._baseline_runs: list[list[TraceEvent]] = []
        for bp in baseline_paths or []:
            self._baseline_runs.append(self._load_trace(Path(bp)))

    @classmethod
    def for_problem(
        cls,
        problem_id: str,
        baseline_paths: list[Path | str] | None = None,
        use_baseline: bool = True,
    ) -> ProcessOracle:
        specs_dir = Path(__file__).parent / "llm_as_a_judge" / "process_specs"
        path = specs_dir / f"{problem_id}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"No process spec found for {problem_id} at {path}")
        if baseline_paths is None and use_baseline:
            baseline_paths = sorted(
                (specs_dir / "baselines").glob(f"{problem_id}__healthy_run*.json")
            )
        return cls(spec_path=path, baseline_paths=baseline_paths or None)

    def evaluate(self, trajectory_path: Path | str) -> ProcessOracleResult:
        trace = self._load_trace(Path(trajectory_path))

        chain = self._spec.get("causal_chain", [])
        chain_results = [
            _evaluate_chain_node(node, trace, self._baseline_runs) for node in chain
        ]

        n = len(chain_results)
        nodes_touched = sum(1 for r in chain_results if r.touched)
        node_f1 = round(nodes_touched / n, 3) if n > 0 else 0.0

        edges_covered = sum(
            1
            for i in range(n - 1)
            if chain_results[i].touched and chain_results[i + 1].touched
        )
        edge_f1 = round(edges_covered / (n - 1), 3) if n > 1 else 0.0

        symptom_reached = chain_results[0].touched if chain_results else False
        root_cause_reached = chain_results[-1].touched if chain_results else False

        passed = symptom_reached and root_cause_reached
        score = round(
            0.3 * float(symptom_reached)
            + 0.4 * node_f1
            + 0.3 * float(root_cause_reached),
            2,
        )

        symptom_unverifiable = (
            _node_unverifiable(chain_results[0]) if chain_results else False
        )
        root_unverifiable = (
            _node_unverifiable(chain_results[-1]) if chain_results else False
        )
        unverifiable_nodes = [r.node_id for r in chain_results if _node_unverifiable(r)]

        rf08 = symptom_reached and not root_cause_reached and not root_unverifiable
        rf05 = root_cause_reached and not symptom_reached and not symptom_unverifiable
        rf13 = _detect_anchoring_risk(self._spec.get("anchoring_traps", []), trace)

        unique_command_count = len({te.command for te in trace if te.command})

        return ProcessOracleResult(
            passed=passed,
            score=score,
            symptom_reached=symptom_reached,
            root_cause_reached=root_cause_reached,
            node_f1=node_f1,
            edge_f1=edge_f1,
            chain_results=chain_results,
            rf08_evidential_insufficiency=rf08,
            rf05_spurious_attribution=rf05,
            rf13_anchoring_risk=rf13,
            report=self._build_report(
                passed,
                score,
                symptom_reached,
                root_cause_reached,
                node_f1,
                edge_f1,
                chain_results,
                rf08,
                rf05,
                rf13,
                trace,
                len(self._baseline_runs),
                unverifiable_nodes,
            ),
            tool_call_count=len(trace),
            unique_command_count=unique_command_count,
            baseline_run_count=len(self._baseline_runs),
            unverifiable_nodes=unverifiable_nodes,
        )

    # ------------------------------------------------------------------
    # Trace loading (ATIF-v1.x)
    # ------------------------------------------------------------------

    def _load_trace(self, path: Path) -> list[TraceEvent]:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not str(data.get("schema_version", "")).startswith("ATIF"):
            raise ValueError(f"{path} is not an ATIF trace (missing or wrong schema_version)")
        return self._extract_trace_atif(data)

    def _extract_trace_atif(self, data: dict) -> list[TraceEvent]:
        trace: list[TraceEvent] = []
        for step in data.get("steps", []):
            if step.get("source") != "agent":
                continue
            tool_calls = step.get("tool_calls") or []
            observation = step.get("observation") or {}
            results = observation.get("results") or []

            result_by_id: dict[str, str] = {}
            for r in results:
                cid = r.get("source_call_id")
                content = r.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if cid is not None:
                    result_by_id[cid] = str(content)

            for tc in tool_calls:
                tool_name = tc.get("function_name", "")
                args = tc.get("arguments") or {}
                call_id = tc.get("tool_call_id", "")
                result = result_by_id.get(call_id, "")
                trace.append(TraceEvent(tool_name=tool_name, args=args, result=result))
        return trace

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _build_report(
        self,
        passed: bool,
        score: float,
        symptom_reached: bool,
        root_cause_reached: bool,
        node_f1: float,
        edge_f1: float,
        chain_results: list[ChainNodeResult],
        rf08: bool,
        rf05: bool,
        rf13: bool,
        trace: list[TraceEvent],
        baseline_run_count: int = 0,
        unverifiable_nodes: list[str] | None = None,
    ) -> str:
        unverifiable_nodes = unverifiable_nodes or []
        unique_cmds = len({te.command for te in trace if te.command})
        lines = [
            f"ProcessOracle: {'PASS' if passed else 'FAIL'} (score={score:.2f})",
            f"Tool calls: {len(trace)}  unique commands: {unique_cmds}",
            f"Node F1: {node_f1:.3f}  Edge F1: {edge_f1:.3f}",
        ]
        if baseline_run_count:
            lines.append(
                f"Baseline gate: {baseline_run_count} healthy run(s) "
                "(signals that also fire on the baseline are rejected)"
            )
        lines += ["", "Causal chain:"]
        for r in chain_results:
            tag = "TOUCH" if r.touched else "miss "
            lines.append(f"  [{tag}] {r.node_id}: {r.component}")
            if r.signals_missed:
                lines.append(f"          missing signals: {r.signals_missed}")
            for pattern, cnt, tot in r.signals_leaked:
                lines.append(
                    f"          baseline-leak (rejected): {pattern!r} "
                    f"matched {cnt}/{tot} healthy run(s)"
                )

        lines += [
            "",
            "Reasoning failures:",
            f"  RF-08 evidential insufficiency:  {'YES' if rf08 else 'no'}",
            f"  RF-05 spurious attribution:      {'YES' if rf05 else 'no'}",
            f"  RF-13 anchoring risk:            {'YES' if rf13 else 'no'}",
        ]
        if unverifiable_nodes:
            lines += [
                "",
                "Spec quality:",
                f"  Non-discriminative nodes (all signals rejected by baseline): "
                f"{unverifiable_nodes}",
                "  These nodes cannot judge the agent until their signals are "
                "tightened;",
                "  reasoning-failure flags for them are suppressed.",
            ]
        return "\n".join(lines)
