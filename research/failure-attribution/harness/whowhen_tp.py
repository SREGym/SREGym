"""Who&When failure attribution with TracePress pipelines, scored the same way for every method.

    python whowhen_tp.py run   --data "Who&When" --subset hand --method map_reduce --lm openrouter:openai/gpt-6-luna
    python whowhen_tp.py score --data "Who&When" --subset hand outputs/a.txt [outputs/b.txt ...]

`run` writes predictions in the text format Automated_FA/evaluate.py reads, so the
baselines' own output files (from Automated_FA/inference.py or run_baselines.py) can be
scored by `score` next to the TracePress ones, with the same metrics.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

SUBSETS = {"hand": ("Hand-Crafted", "role"), "algo": ("Algorithm-Generated", "name")}
METHODS = ("all_at_once", "map_reduce", "fold")

DECISIVE = (
    "This multi-agent conversation failed to solve the problem. The decisive error is the first "
    "mistake that is directly responsible for the wrong final answer. Steps are numbered from 0."
)


def normalize(agent: str) -> str:
    """'Orchestrator (thought)' and 'Orchestrator (-> WebSurfer)' are both the Orchestrator."""
    return re.sub(r"\s*\(.*\)$", "", str(agent)).strip()


def load(data: Path, subset: str) -> dict[str, dict]:
    folder, field = SUBSETS[subset]
    traces = {}
    for path in sorted((data / folder).glob("*.json"), key=lambda p: int(p.stem)):
        trace = json.loads(path.read_text())
        trace["speakers"] = [entry.get(field, "Unknown") for entry in trace["history"]]
        traces[path.name] = trace
    return traces


# --- TracePress pipelines --------------------------------------------------------------


def steps_table(traces: dict[str, dict], with_gt: bool):
    import tracepress as tp

    rows = []
    for tid, trace in traces.items():
        for i, (who, entry) in enumerate(zip(trace["speakers"], trace["history"], strict=True)):
            values = {"question": trace["question"], "step": i, "agent": who, "content": entry.get("content", "")}
            if with_gt:
                values["ground_truth"] = str(trace["ground_truth"])
            rows.append(tp.Row((("trace", tid), ("step", i)), values))
    return tp.from_rows(rows)


def pipeline(steps, method: str, with_gt: bool):
    gt = "ground_truth, " if with_gt else ""
    keys = ("trace", "question", *(("ground_truth",) if with_gt else ()))
    if method == "all_at_once":
        # The paper's all-at-once, except that traces over the chunk budget are tree-reduced.
        return steps.group_by(*keys).lm_reduce(
            f"question, {gt}step, agent, content -> mistake_step: int, mistake_agent, reason",
            instructions=DECISIVE + " Name that step and the agent who made it.",
        )
    if method == "map_reduce":
        # Judge every step on its own (in parallel), then pick the decisive one from the notes.
        notes = steps.lm_map(
            f"question, {gt}step, agent, content -> what_happened, is_mistake: bool, why",
            instructions=(
                "You are reviewing one step of a multi-agent conversation that failed to solve the problem. "
                "Say what this agent did, and whether it contains an error that could derail solving the problem. "
                "Don't be overly critical: flag only errors that clearly derail the process."
            ),
        )
        return notes.group_by(*keys).lm_reduce(
            f"question, {gt}step, agent, what_happened, is_mistake, why -> mistake_step: int, mistake_agent, reason",
            instructions=DECISIVE + " You see one note per step, in order. Several steps may be flagged: "
            "pick the one the failure traces back to, and name its agent.",
        )
    if method == "fold":
        # Step-by-step with a bounded running note instead of the whole history prefix.
        return steps.group_by(*keys).lm_fold(
            f"notes, mistake_step, mistake_agent, question, {gt}step, agent, content -> "
            "notes, mistake_step: int, mistake_agent",
            init={"notes": "", "mistake_step": -1, "mistake_agent": ""},
            instructions=DECISIVE + " You read the conversation one step at a time. Keep short notes on progress, "
            "and keep mistake_step / mistake_agent on the earliest step so far that looks like the decisive "
            "error (-1 while none does). Change them only for an earlier or clearly more decisive error.",
        )
    raise ValueError(f"unknown method {method!r}; choose from {METHODS}")


def run(args) -> None:
    import tracepress as tp

    tp.configure(lm=args.lm, telemetry=False)
    traces = load(Path(args.data), args.subset)
    if args.limit:
        traces = dict(list(traces.items())[: args.limit])
    with_gt = not args.no_gt
    rows = pipeline(steps_table(traces, with_gt), args.method, with_gt).collect()

    blocks = []
    for row in sorted(rows, key=lambda r: int(Path(r.trace).stem)):
        values = row.to_dict()
        blocks.append(
            f"Prediction for {row.trace}:\n"
            f"Agent Name: {normalize(values.get('mistake_agent', '')).replace(' ', '_')}\n"
            f"Step Number: {values.get('mistake_step', -1)}\n"
            f"Reason for Mistake: {values.get('reason', values.get('notes', ''))}\n"
        )
    gt_tag = "" if with_gt else "_nogt"
    out = Path(args.out or f"outputs/tp_{args.method}_{args.subset}{gt_tag}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(blocks))
    print(f"wrote {len(blocks)} of {len(traces)} predictions to {out}")
    print(tp.last_run())


# --- Scoring ---------------------------------------------------------------------------


def parse(eval_file: Path) -> dict[str, tuple[str, int | None]]:
    """Same block format and regexes as Automated_FA/evaluate.py, except that agent names may
    contain hyphens (its `[\\w_]+` cuts the one label `Blu-Ray_Expert` short)."""
    text = eval_file.read_text(encoding="utf-8")
    preds = {}
    for block in re.finditer(r"Prediction for ([^:]+\.json):(.*?)(?=Prediction for|\Z)", text, re.S):
        agent = re.search(r"Agent Name:\s*([\w-]+)", block.group(2), re.I)
        step = re.search(r"Step Number:\s*(-?\d+)", block.group(2), re.I)
        if agent and step:
            preds[block.group(1).strip()] = (agent.group(1), int(step.group(1)))
    return preds


def correctness(traces: dict[str, dict], preds: dict, tol: int) -> dict[str, list[bool]]:
    """Per-trace hits for each metric. A trace with no parsed prediction is a miss everywhere."""
    hits = {m: [] for m in ("agent", "agent_from_step", "step", f"step_pm{tol}", "paper_agent", "paper_step")}
    for tid, trace in traces.items():
        true_agent, true_step = trace["mistake_agent"], int(trace["mistake_step"])
        agent, step = preds.get(tid, (None, None))
        found = agent is not None
        speaker = normalize(trace["speakers"][step]) if found and 0 <= step < len(trace["speakers"]) else None
        hits["agent"].append(found and normalize(agent) == true_agent)
        hits["agent_from_step"].append(speaker == true_agent)
        hits["step"].append(found and step == true_step)
        hits[f"step_pm{tol}"].append(found and abs(step - true_step) <= tol)
        # evaluate.py checks substring containment, so e.g. a true step 1 matches a predicted 12.
        hits["paper_agent"].append(found and true_agent in agent)
        hits["paper_step"].append(found and str(true_step) in str(step))
    return hits


def bootstrap(xs: list[bool], n: int = 2000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def paired_diff(a: list[bool], b: list[bool], n: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """b - a accuracy on the same traces, with a paired bootstrap 95% interval."""
    rng = random.Random(seed)
    idx = range(len(a))
    diffs = sorted(
        sum(b[i] - a[i] for i in sample) / len(a) for sample in (rng.choices(idx, k=len(a)) for _ in range(n))
    )
    return sum(b) / len(b) - sum(a) / len(a), diffs[int(0.025 * n)], diffs[int(0.975 * n)]


def score(args) -> None:
    traces = load(Path(args.data), args.subset)
    results = {}
    for name in args.eval_files:
        preds = parse(Path(name))
        results[name] = hits = correctness(traces, preds, args.tol)
        print(f"\n{name}  ({len(preds)}/{len(traces)} predictions parsed)")
        for metric, xs in hits.items():
            lo, hi = bootstrap(xs)
            print(f"  {metric:16} {100 * sum(xs) / len(xs):6.2f}%   95% CI [{100 * lo:5.1f}, {100 * hi:5.1f}]")
    if len(args.eval_files) > 1:
        base, *others = args.eval_files
        print(f"\nPaired differences vs {base}:")
        for name in others:
            for metric in ("agent", "agent_from_step", "step"):
                d, lo, hi = paired_diff(results[base][metric], results[name][metric])
                print(f"  {name:40} {metric:16} {100 * d:+6.2f} pts   95% CI [{100 * lo:+5.1f}, {100 * hi:+5.1f}]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("run", "score"):
        p = sub.add_parser(cmd)
        p.add_argument("--data", required=True, help="path to the Who&When folder")
        p.add_argument("--subset", choices=SUBSETS, required=True)
    p_run = sub.choices["run"]
    p_run.add_argument("--method", choices=METHODS, required=True)
    p_run.add_argument("--lm", required=True, help="e.g. openrouter:openai/gpt-6-luna")
    p_run.add_argument("--no-gt", action="store_true", help="hide the ground-truth answer (the paper's harder setting)")
    p_run.add_argument("--limit", type=int, help="only the first N traces, for a cheap smoke test")
    p_run.add_argument("--out")
    p_score = sub.choices["score"]
    p_score.add_argument("eval_files", nargs="+")
    p_score.add_argument("--tol", type=int, default=2)
    args = parser.parse_args()
    run(args) if args.cmd == "run" else score(args)


if __name__ == "__main__":
    main()
