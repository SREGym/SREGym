import json
from pathlib import Path
from collections import defaultdict, Counter

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import matplotlib.pyplot as plt
from collections import Counter
from collections import Counter
import numpy as np
import matplotlib.pyplot as plt

all_results_csv_path = (
    Path(__file__).parent / "stratus_12-29_09-34_resource_request_too_large_results.csv"
)
all_results_csv = pd.read_csv(all_results_csv_path)
pd.set_option("display.max_columns", None)



def extract_tool_calls(msg: dict):
    """
    Normalizes tool calls to:
      [{"name": <tool_name>, "args": <raw_args_or_dict>}, ...]
    Supports:
      - msg["tool_calls"] (OpenAI-style)
      - msg["additional_kwargs"]["tool_calls"] (LangChain function-style)
    """
    tcs = msg.get("tool_calls", [])
    if isinstance(tcs, list) and tcs:
        normalized = []
        for tc in tcs:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name") if isinstance(fn, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else None

            if not name and isinstance(tc, dict):
                name = tc.get("name")
            if args is None and isinstance(tc, dict):
                args = tc.get("args")

            if name:
                normalized.append({"name": name, "args": args})
        return normalized

    ak = msg.get("additional_kwargs", {})
    if isinstance(ak, dict):
        tcs2 = ak.get("tool_calls")
        if isinstance(tcs2, list) and tcs2:
            normalized = []
            for tc in tcs2:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name") if isinstance(fn, dict) else None
                args = fn.get("arguments") if isinstance(fn, dict) else None

                if not name and isinstance(tc, dict):
                    name = tc.get("name")
                if args is None and isinstance(tc, dict):
                    args = tc.get("args")

                if name:
                    normalized.append({"name": name, "args": args})
            return normalized

    return []


def load_jsonl_into_df(problem_id: str) -> pd.DataFrame:
    rows = []
    print(f"Loading JSONL for problem_id: {problem_id}")

    matches = list(Path(__file__).parent.rglob(f"*{problem_id}*.jsonl"))
    if not matches:
        raise FileNotFoundError(f"No JSONL found containing problem_id={problem_id}")

    jsonl_path = matches[0]
    with open(jsonl_path, "r") as file:
        first_obj = json.loads(next(file))
        problem = first_obj.get("problem_id", "")

        for line in file:
            obj = json.loads(line)
            stage = obj.get("stage", "")
            num_steps = obj.get("num_steps", 0)
            messages = obj.get("messages", [])

            for msg in messages:
                rows.append(
                    {
                        "problem_id": problem,
                        "types": msg.get("type", ""),
                        "contents": msg.get("content", ""),
                        "tool_calls": extract_tool_calls(msg),
                        "stage": stage,
                        "num_steps": num_steps,
                    }
                )

    print(f"Loaded {jsonl_path} with problem_id: {problem}")
    return pd.DataFrame(rows)


problem_ids = all_results_csv["problem_id"].unique()
problem_dfs = {}
for pid in problem_ids:
    try:
        problem_dfs[pid] = load_jsonl_into_df(pid)
    except Exception as e:
        print(f"Error loading problem_id {pid}: {e}")
        continue



def successful(problem_id, stage=None) -> bool:
    row = all_results_csv.loc[all_results_csv["problem_id"] == problem_id].iloc[0]

    if stage is None:
        return bool(row["Mitigation.success"]) and bool(row["Diagnosis.success"])

    col = "Diagnosis.success" if stage == "localization" else "Mitigation.success"
    return bool(row[col])


def not_successful(problem_id, stage=None) -> bool:
    row = all_results_csv.loc[all_results_csv["problem_id"] == problem_id].iloc[0]

    if stage is None:
        return (not bool(row["Mitigation.success"])) or (not bool(row["Diagnosis.success"]))

    col = "Diagnosis.success" if stage == "localization" else "Mitigation.success"
    return not bool(row[col])


def _passes_filter(problem_id, stage, filter_mode) -> bool:
    """
    filter_mode:
      - None: no filtering
      - "success": only successful for that stage (or overall if stage=None)
      - "fail": only not-successful for that stage (or overall if stage=None)
    """
    if filter_mode is None:
        return True
    if filter_mode == "success":
        return successful(problem_id, stage)
    if filter_mode == "fail":
        return not_successful(problem_id, stage)
    raise ValueError("filter_mode must be None, 'success', or 'fail'")



def iter_step_tool_calls(stage_df: pd.DataFrame):
    """
    Yields (step_num, merged_tool_calls_list)

    IMPORTANT: De-duplicates repeated rows with the same num_steps by
    aggregating tool_calls across those rows once per unique num_steps.
    """
    if stage_df.empty:
        return
    for step, grp in stage_df.groupby("num_steps", dropna=True, sort=False):
        merged = []
        for cell in grp["tool_calls"]:
            if isinstance(cell, list) and cell:
                merged.extend(cell)
        yield step, merged



def problem_with_max_steps(problem_dfs, stage, filter_mode=None):
    max_steps = -1
    max_problem_id = None
    if not stage:
        return None, -1

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        steps = stage_df["num_steps"].max()
        if pd.isna(steps):
            continue
        if steps > max_steps:
            max_steps = steps
            max_problem_id = problem_id

    return max_problem_id, max_steps


def problem_with_min_steps(problem_dfs, stage, filter_mode=None):
    min_steps = float("inf")
    min_problem_id = None
    if not stage:
        return None, float("inf")

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        steps = stage_df["num_steps"].max()
        if pd.isna(steps):
            continue
        if steps < min_steps:
            min_steps = steps
            min_problem_id = problem_id

    return min_problem_id, min_steps


def total_maximum_steps(problem_dfs, filter_mode=None):
    """
    Totals max num_steps per stage for each problem_id, sums across stages.
    filter_mode applies OVERALL (stage=None), not per-stage.
    """
    if not problem_dfs:
        return None, 0, {}

    stages = problem_dfs[next(iter(problem_dfs))]["stage"].dropna().unique()
    problem_id_to_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage=None, filter_mode=filter_mode):
            continue

        total = 0
        for stg in stages:
            stage_df = df[df["stage"] == stg]
            steps = stage_df["num_steps"].max()
            total += 0 if pd.isna(steps) else int(steps)

        problem_id_to_count[problem_id] = total

    if not problem_id_to_count:
        return None, 0, {}

    max_problem_id = max(problem_id_to_count, key=problem_id_to_count.get)
    return max_problem_id, problem_id_to_count[max_problem_id], problem_id_to_count


def total_minimum_steps(problem_dfs, filter_mode=None):
    """
    Totals max num_steps per stage for each problem_id, sums across stages.
    filter_mode applies OVERALL (stage=None), not per-stage.
    """
    if not problem_dfs:
        return None, 0, {}

    stages = problem_dfs[next(iter(problem_dfs))]["stage"].dropna().unique()
    problem_id_to_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage=None, filter_mode=filter_mode):
            continue

        total = 0
        for stg in stages:
            stage_df = df[df["stage"] == stg]
            steps = stage_df["num_steps"].max()
            total += 0 if pd.isna(steps) else int(steps)

        problem_id_to_count[problem_id] = total

    if not problem_id_to_count:
        return None, 0, {}

    min_problem_id = min(problem_id_to_count, key=problem_id_to_count.get)
    return min_problem_id, problem_id_to_count[min_problem_id], problem_id_to_count


def avg_steps_per_stage(problem_dfs, stage, filter_mode=None):
    total_steps = 0
    count = 0
    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        steps = stage_df["num_steps"].max()
        if not pd.isna(steps):
            total_steps += int(steps)
            count += 1
    return total_steps / count if count > 0 else 0



def most_frequently_used_tool(problem_dfs, stage, filter_mode=None):
    """
    Counts "tool used in how many unique steps" within a stage.
    (Binary per step per tool)
    """
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        for step, tool_calls in iter_step_tool_calls(stage_df):
            tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
            for name in tools_in_step:
                tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    most_used_tool = max(tool_count, key=tool_count.get)
    return most_used_tool, tool_count[most_used_tool]


def least_frequently_used_tool(problem_dfs, stage, filter_mode=None):
    """
    Counts "tool used in how many unique steps" within a stage.
    (Binary per step per tool)
    """
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        for step, tool_calls in iter_step_tool_calls(stage_df):
            tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
            for name in tools_in_step:
                tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    least_used_tool = min(tool_count, key=tool_count.get)
    return least_used_tool, tool_count[least_used_tool]


def total_most_frequently_used_tool(problem_dfs, filter_mode=None):
    """
    Counts "tool used in how many unique steps" across all stages.
    (Binary per step per tool, summed across all (problem_id, stage, step))
    filter_mode applies OVERALL (stage=None).
    """
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage=None, filter_mode=filter_mode):
            continue

        for stg in df["stage"].dropna().unique():
            stage_df = df[df["stage"] == stg]
            for step, tool_calls in iter_step_tool_calls(stage_df):
                tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
                for name in tools_in_step:
                    tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    most_used_tool = max(tool_count, key=tool_count.get)
    return most_used_tool, tool_count[most_used_tool]


def total_least_frequently_used_tool(problem_dfs, filter_mode=None):
    """
    Counts "tool used in how many unique steps" across all stages.
    (Binary per step per tool, summed across all (problem_id, stage, step))
    filter_mode applies OVERALL (stage=None).
    """
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage=None, filter_mode=filter_mode):
            continue

        for stg in df["stage"].dropna().unique():
            stage_df = df[df["stage"] == stg]
            for step, tool_calls in iter_step_tool_calls(stage_df):
                tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
                for name in tools_in_step:
                    tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    least_used_tool = min(tool_count, key=tool_count.get)
    return least_used_tool, tool_count[least_used_tool]


def step_to_tool_call(problem_dfs, filter_mode=None):
    """
    Returns: dict[int, Counter]
      { step_num: Counter({tool_name: count, ...}), ... }

    Counts tools once per unique num_steps per (problem_id, stage) via iter_step_tool_calls().
    filter_mode applies OVERALL (stage=None) to include only success/fail runs if desired.
    """
    tool_count_per_step = defaultdict(Counter)

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(problem_id, stage=None, filter_mode=filter_mode):
            continue

        for stg in df["stage"].dropna().unique():
            stage_df = df[df["stage"] == stg]
            for step, tool_calls in iter_step_tool_calls(stage_df):
                tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
                for name in tools_in_step:
                    tool_count_per_step[int(step)][name] += 1

    return dict(tool_count_per_step)



def plot_tool_usage_by_step(
    tool_count_per_step,
    top_k_tools=None,
    gap=6.0,
    width=None,
    title="Tool usage by step",
    save_path=None,
    dpi=160,
    show=True,
):
    """
    Stacked bar histogram of tool usage across steps.

    - Bars thicker (width defaults to 72% of gap)
    - Shows ALL ticks, not tilted
    - Legend outside
    - Optional save to PNG
    """
    if not tool_count_per_step:
        print("No tool counts to plot.")
        return None, None

    steps = sorted(tool_count_per_step.keys())
    n = len(steps)

    # Choose tools (optionally top-k)
    total_by_tool = Counter()
    for s in steps:
        total_by_tool.update(tool_count_per_step[s])

    tools = (
        [t for t, _ in total_by_tool.most_common(top_k_tools)]
        if top_k_tools is not None
        else [t for t, _ in total_by_tool.most_common()]
    )

    counts = {
        tool: np.array([tool_count_per_step[s].get(tool, 0) for s in steps], dtype=int)
        for tool in tools
    }

    # Spaced x positions
    x = np.arange(n) * gap

    # Chunky bars
    if width is None:
        width = 0.72 * gap

    # Auto-size figure
    fig_w = max(14, n * gap * 0.55)
    fig_h = 7

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), constrained_layout=True)

    bottom = np.zeros(n, dtype=int)
    for tool in tools:
        y = counts[tool]
        ax.bar(x, y, bottom=bottom, width=width, label=tool)
        bottom += y

    ax.set_title(title)
    ax.set_xlabel("Step (Iteration)")
    ax.set_ylabel("Frequency")

    ax.set_xticks(x)
    ax.set_xticklabels([str(s) for s in steps], rotation=0, ha="center", fontsize=8)

    ymax = int(bottom.max()) if bottom.size else 0
    ax.set_ylim(0, ymax * 1.12 if ymax > 0 else 1)

    ax.grid(axis="y", linewidth=0.5, alpha=0.3)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=True)

    if save_path:
        fig.savefig(save_path, dpi=dpi, bbox_inches="tight")

    if show:
        plt.show()
    else:
        plt.close(fig)

    return fig, ax



# ---------- ALL (no filter) ----------
max_problem_id, max_steps = problem_with_max_steps(problem_dfs, stage="localization", filter_mode=None)
print(f"Problem with max steps in Localization stage: {max_problem_id} with {max_steps} steps")

max_problem_id, max_steps = problem_with_max_steps(problem_dfs, stage="mitigation_attempt_0", filter_mode=None)
print(f"Problem with max steps in mitigation_attempt_0 stage: {max_problem_id} with {max_steps} steps")

max_problem_id, max_steps, problem_id_to_count = total_maximum_steps(problem_dfs, filter_mode=None)
print(f"Problem with max total steps across all stages: {max_problem_id} with {max_steps} steps")

avg_localization_steps = avg_steps_per_stage(problem_dfs, stage="localization", filter_mode=None)
print(f"Average steps in Localization stage: {avg_localization_steps}")

avg_mitigation_steps = avg_steps_per_stage(problem_dfs, stage="mitigation_attempt_0", filter_mode=None)
print(f"Average steps in mitigation_attempt_0 stage: {avg_mitigation_steps}")

min_problem_id, min_steps = problem_with_min_steps(problem_dfs, stage="localization", filter_mode=None)
print(f"Problem with min steps in Localization stage: {min_problem_id} with {min_steps} steps")

min_problem_id, min_steps, problem_id_to_count = total_minimum_steps(problem_dfs, filter_mode=None)
print(f"Problem with min total steps across all stages: {min_problem_id} with {min_steps} steps")

most_used_tool, count = most_frequently_used_tool(problem_dfs, stage="localization", filter_mode=None)
print(f"Most frequently used tool in Localization stage (unique num_steps): {most_used_tool} used in {count} steps")

most_used_tool, count = most_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode=None)
print(f"Most frequently used tool in mitigation_attempt_0 stage (unique num_steps): {most_used_tool} used in {count} steps")

most_used_tool, count = total_most_frequently_used_tool(problem_dfs, filter_mode=None)
print(f"Most frequently used tool across all stages (unique num_steps): {most_used_tool} used in {count} steps")

least_used_tool, count = least_frequently_used_tool(problem_dfs, stage="localization", filter_mode=None)
print(f"Least frequently used tool in Localization stage (unique num_steps): {least_used_tool} used in {count} steps")

least_used_tool, count = least_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode=None)
print(f"Least frequently used tool in mitigation_attempt_0 stage (unique num_steps): {least_used_tool} used in {count} steps")

least_used_tool, count = total_least_frequently_used_tool(problem_dfs, filter_mode=None)
print(f"Least frequently used tool across all stages (unique num_steps): {least_used_tool} used in {count} steps")


# ---------- SUCCESS ONLY ----------
min_problem_id, min_steps = problem_with_min_steps(problem_dfs, stage="localization", filter_mode="success")
print(f"Problem with min steps in localization stage (successful only): {min_problem_id} with {min_steps} steps")

min_problem_id, min_steps = problem_with_min_steps(problem_dfs, stage="mitigation_attempt_0", filter_mode="success")
print(f"Problem with min steps in mitigation_attempt_0 stage (successful only): {min_problem_id} with {min_steps} steps")

max_problem_id, max_steps = problem_with_max_steps(problem_dfs, stage="localization", filter_mode="success")
print(f"Problem with max steps in Localization stage (successful only): {max_problem_id} with {max_steps} steps")

max_problem_id, max_steps = problem_with_max_steps(problem_dfs, stage="mitigation_attempt_0", filter_mode="success")
print(f"Problem with max steps in mitigation_attempt_0 stage (successful only): {max_problem_id} with {max_steps} steps")

most_used_tool, count = most_frequently_used_tool(problem_dfs, stage="localization", filter_mode="success")
print(f"Most frequently used tool in Localization stage (successful only, unique num_steps): {most_used_tool} used in {count} steps")

most_used_tool, count = most_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode="success")
print(f"Most frequently used tool in mitigation_attempt_0 stage (successful only, unique num_steps): {most_used_tool} used in {count} steps")

most_used_tool, count = total_most_frequently_used_tool(problem_dfs, filter_mode="success")
print(f"Most frequently used tool across all stages (successful only, unique num_steps): {most_used_tool} used in {count} steps")

least_used_tool, count = least_frequently_used_tool(problem_dfs, stage="localization", filter_mode="success")
print(f"Least frequently used tool in Localization stage (successful only, unique num_steps): {least_used_tool} used in {count} steps")

least_used_tool, count = least_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode="success")
print(f"Least frequently used tool in mitigation_attempt_0 stage (successful only, unique num_steps): {least_used_tool} used in {count} steps")

least_used_tool, count = total_least_frequently_used_tool(problem_dfs, filter_mode="success")
print(f"Least frequently used tool across all stages (successful only, unique num_steps): {least_used_tool} used in {count} steps")


# ---------- FAIL ONLY (NEW) ----------
min_problem_id, min_steps = problem_with_min_steps(problem_dfs, stage="localization", filter_mode="fail")
print(f"Problem with min steps in localization stage (fail only): {min_problem_id} with {min_steps} steps")

min_problem_id, min_steps = problem_with_min_steps(problem_dfs, stage="mitigation_attempt_0", filter_mode="fail")
print(f"Problem with min steps in mitigation_attempt_0 stage (fail only): {min_problem_id} with {min_steps} steps")

max_problem_id, max_steps = problem_with_max_steps(problem_dfs, stage="localization", filter_mode="fail")
print(f"Problem with max steps in Localization stage (fail only): {max_problem_id} with {max_steps} steps")

max_problem_id, max_steps = problem_with_max_steps(problem_dfs, stage="mitigation_attempt_0", filter_mode="fail")
print(f"Problem with max steps in mitigation_attempt_0 stage (fail only): {max_problem_id} with {max_steps} steps")

most_used_tool, count = most_frequently_used_tool(problem_dfs, stage="localization", filter_mode="fail")
print(f"Most frequently used tool in Localization stage (fail only, unique num_steps): {most_used_tool} used in {count} steps")

most_used_tool, count = most_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode="fail")
print(f"Most frequently used tool in mitigation_attempt_0 stage (fail only, unique num_steps): {most_used_tool} used in {count} steps")

most_used_tool, count = total_most_frequently_used_tool(problem_dfs, filter_mode="fail")
print(f"Most frequently used tool across all stages (fail only, unique num_steps): {most_used_tool} used in {count} steps")

least_used_tool, count = least_frequently_used_tool(problem_dfs, stage="localization", filter_mode="fail")
print(f"Least frequently used tool in Localization stage (fail only, unique num_steps): {least_used_tool} used in {count} steps")

least_used_tool, count = least_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode="fail")
print(f"Least frequently used tool in mitigation_attempt_0 stage (fail only, unique num_steps): {least_used_tool} used in {count} steps")

least_used_tool, count = total_least_frequently_used_tool(problem_dfs, filter_mode="fail")
print(f"Least frequently used tool across all stages (fail only, unique num_steps): {least_used_tool} used in {count} steps")
#plot_tool_usage_by_step(tool_calls_per_step, top_k_tools=10)

print("Done.")
# ----------------------------
# Pretty report (terminal + HTML + figure)
# ----------------------------
import base64
from datetime import datetime
from html import escape as html_escape


def _safe(v):
    return "" if v is None else v


def _mode_label(mode: str) -> str:
    return {"all": "ALL", "success": "SUCCESS", "fail": "FAIL"}[mode]


def _mode_suffix(mode: str) -> str:
    return "" if mode == "all" else f"_{mode}"


def collect_summary(problem_dfs):
    """
    Build a summary dict with metrics for:
      - all     (suffix: "")
      - success (suffix: "_success")
      - fail    (suffix: "_fail")
    """
    summary = {}
    modes = ["all", "success", "fail"]

    for mode in modes:
        filter_mode = None if mode == "all" else mode
        suf = _mode_suffix(mode)

        # ---- Step metrics ----
        pid, steps = problem_with_max_steps(problem_dfs, stage="localization", filter_mode=filter_mode)
        summary[f"max_steps_localization{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps = problem_with_max_steps(problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"max_steps_mitigation_0{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps, _ = total_maximum_steps(problem_dfs, filter_mode=filter_mode)
        summary[f"max_total_steps_all_stages{suf}"] = {"problem_id": pid, "steps": steps}

        summary[f"avg_steps_localization{suf}"] = avg_steps_per_stage(problem_dfs, stage="localization", filter_mode=filter_mode)
        summary[f"avg_steps_mitigation_0{suf}"] = avg_steps_per_stage(problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)

        pid, steps = problem_with_min_steps(problem_dfs, stage="localization", filter_mode=filter_mode)
        summary[f"min_steps_localization{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps = problem_with_min_steps(problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"min_steps_mitigation_0{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps, _ = total_minimum_steps(problem_dfs, filter_mode=filter_mode)
        summary[f"min_total_steps_all_stages{suf}"] = {"problem_id": pid, "steps": steps}

        # ---- Tool frequency metrics ----
        tool, c = most_frequently_used_tool(problem_dfs, stage="localization", filter_mode=filter_mode)
        summary[f"most_used_tool_localization{suf}"] = {"tool": tool, "steps": c}

        tool, c = most_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"most_used_tool_mitigation_0{suf}"] = {"tool": tool, "steps": c}

        tool, c = total_most_frequently_used_tool(problem_dfs, filter_mode=filter_mode)
        summary[f"most_used_tool_all_stages{suf}"] = {"tool": tool, "steps": c}

        tool, c = least_frequently_used_tool(problem_dfs, stage="localization", filter_mode=filter_mode)
        summary[f"least_used_tool_localization{suf}"] = {"tool": tool, "steps": c}

        tool, c = least_frequently_used_tool(problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"least_used_tool_mitigation_0{suf}"] = {"tool": tool, "steps": c}

        tool, c = total_least_frequently_used_tool(problem_dfs, filter_mode=filter_mode)
        summary[f"least_used_tool_all_stages{suf}"] = {"tool": tool, "steps": c}

    return summary


def pretty_print_summary(summary: dict):
    """Terminal summary with a Mode column: ALL vs SUCCESS vs FAIL."""
    modes = ["all", "success", "fail"]
    rows = []

    for mode in modes:
        suf = _mode_suffix(mode)
        m = _mode_label(mode)

        rows.extend([
            (m, "Max steps (Localization)",
             summary[f"max_steps_localization{suf}"]["problem_id"],
             summary[f"max_steps_localization{suf}"]["steps"]),

            (m, "Max steps (Mitigation 0)",
             summary[f"max_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"max_steps_mitigation_0{suf}"]["steps"]),

            (m, "Max total steps (All stages)",
             summary[f"max_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"max_total_steps_all_stages{suf}"]["steps"]),

            (m, "Avg steps (Localization)", "-", f'{summary[f"avg_steps_localization{suf}"]:.2f}'),
            (m, "Avg steps (Mitigation 0)", "-", f'{summary[f"avg_steps_mitigation_0{suf}"]:.2f}'),

            (m, "Min steps (Localization)",
             summary[f"min_steps_localization{suf}"]["problem_id"],
             summary[f"min_steps_localization{suf}"]["steps"]),

            (m, "Min steps (Mitigation 0)",
             summary[f"min_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"min_steps_mitigation_0{suf}"]["steps"]),

            (m, "Min total steps (All stages)",
             summary[f"min_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"min_total_steps_all_stages{suf}"]["steps"]),

            (m, "Most used tool (Localization)",
             summary[f"most_used_tool_localization{suf}"]["tool"],
             summary[f"most_used_tool_localization{suf}"]["steps"]),

            (m, "Most used tool (Mitigation 0)",
             summary[f"most_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"most_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Most used tool (All stages)",
             summary[f"most_used_tool_all_stages{suf}"]["tool"],
             summary[f"most_used_tool_all_stages{suf}"]["steps"]),

            (m, "Least used tool (Localization)",
             summary[f"least_used_tool_localization{suf}"]["tool"],
             summary[f"least_used_tool_localization{suf}"]["steps"]),

            (m, "Least used tool (Mitigation 0)",
             summary[f"least_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"least_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Least used tool (All stages)",
             summary[f"least_used_tool_all_stages{suf}"]["tool"],
             summary[f"least_used_tool_all_stages{suf}"]["steps"]),
        ])

    try:
        from tabulate import tabulate
        print("\n" + tabulate(rows, headers=["Mode", "Metric", "Item", "Value"], tablefmt="rounded_grid"))
    except Exception:
        col0 = max(len(str(r[0])) for r in rows) + 2
        col1 = max(len(str(r[1])) for r in rows) + 2
        col2 = max(len(str(r[2])) for r in rows) + 2
        print("\n" + "=" * (col0 + col1 + col2 + 14))
        print(f'{"Mode":<{col0}}{"Metric":<{col1}}{"Item":<{col2}}{"Value":>12}')
        print("-" * (col0 + col1 + col2 + 14))
        for mode, metric, item, val in rows:
            print(f"{mode:<{col0}}{metric:<{col1}}{str(item):<{col2}}{str(val):>12}")
        print("=" * (col0 + col1 + col2 + 14))


def write_html_report(
    summary: dict,
    fig_path: str,
    out_path: str = "analysis_report.html",
    title: str = "Stratus Evaluation Report",
):
    """Single-file HTML report with embedded figure (base64)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    img_b64 = ""
    try:
        with open(fig_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        img_b64 = ""
        print(f"Warning: couldn't read figure at {fig_path}: {e}")

    modes = ["all", "success", "fail"]
    table_rows = []

    for mode in modes:
        suf = _mode_suffix(mode)
        m = _mode_label(mode)

        table_rows.extend([
            (m, "Max steps (Localization)",
             summary[f"max_steps_localization{suf}"]["problem_id"],
             summary[f"max_steps_localization{suf}"]["steps"]),

            (m, "Max steps (Mitigation 0)",
             summary[f"max_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"max_steps_mitigation_0{suf}"]["steps"]),

            (m, "Max total steps (All stages)",
             summary[f"max_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"max_total_steps_all_stages{suf}"]["steps"]),

            (m, "Avg steps (Localization)", "-", f'{summary[f"avg_steps_localization{suf}"]:.2f}'),
            (m, "Avg steps (Mitigation 0)", "-", f'{summary[f"avg_steps_mitigation_0{suf}"]:.2f}'),

            (m, "Min steps (Localization)",
             summary[f"min_steps_localization{suf}"]["problem_id"],
             summary[f"min_steps_localization{suf}"]["steps"]),

            (m, "Min steps (Mitigation 0)",
             summary[f"min_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"min_steps_mitigation_0{suf}"]["steps"]),

            (m, "Min total steps (All stages)",
             summary[f"min_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"min_total_steps_all_stages{suf}"]["steps"]),

            (m, "Most used tool (Localization)",
             summary[f"most_used_tool_localization{suf}"]["tool"],
             summary[f"most_used_tool_localization{suf}"]["steps"]),

            (m, "Most used tool (Mitigation 0)",
             summary[f"most_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"most_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Most used tool (All stages)",
             summary[f"most_used_tool_all_stages{suf}"]["tool"],
             summary[f"most_used_tool_all_stages{suf}"]["steps"]),

            (m, "Least used tool (Localization)",
             summary[f"least_used_tool_localization{suf}"]["tool"],
             summary[f"least_used_tool_localization{suf}"]["steps"]),

            (m, "Least used tool (Mitigation 0)",
             summary[f"least_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"least_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Least used tool (All stages)",
             summary[f"least_used_tool_all_stages{suf}"]["tool"],
             summary[f"least_used_tool_all_stages{suf}"]["steps"]),
        ])
    def card(label, value):
        return f"""
        <div class="card">
          <div class="label">{html_escape(label)}</div>
          <div class="value">{html_escape(str(value))}</div>
        </div>
        """

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html_escape(title)}</title>
  <style>
    :root {{
      --bg: #0b1220;
      --panel: rgba(255,255,255,0.06);
      --panel2: rgba(255,255,255,0.08);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.68);
      --stroke: rgba(255,255,255,0.12);
      --shadow: 0 12px 32px rgba(0,0,0,0.35);
      --radius: 18px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
      background: radial-gradient(1200px 600px at 20% 10%, rgba(88,101,242,0.25), transparent 55%),
                  radial-gradient(1000px 500px at 80% 20%, rgba(34,197,94,0.18), transparent 60%),
                  var(--bg);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 28px 18px 60px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: baseline;
      margin-bottom: 18px;
    }}
    h1 {{
      font-size: 22px;
      margin: 0;
      letter-spacing: 0.2px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 14px 0 18px;
    }}
    .card {{
      background: linear-gradient(180deg, var(--panel2), var(--panel));
      border: 1px solid var(--stroke);
      border-radius: var(--radius);
      padding: 14px 14px 12px;
      box-shadow: var(--shadow);
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .value {{
      font-size: 18px;
      font-weight: 650;
      line-height: 1.1;
    }}
    .panel {{
      background: linear-gradient(180deg, var(--panel2), var(--panel));
      border: 1px solid var(--stroke);
      border-radius: var(--radius);
      padding: 16px;
      box-shadow: var(--shadow);
      margin-top: 12px;
    }}
    .panel h2 {{
      margin: 0 0 12px;
      font-size: 16px;
      letter-spacing: 0.2px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      overflow: hidden;
      border-radius: 14px;
      border: 1px solid var(--stroke);
    }}
    th, td {{
      padding: 10px 10px;
      border-bottom: 1px solid var(--stroke);
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      background: rgba(255,255,255,0.05);
    }}
    tr:last-child td {{ border-bottom: none; }}
    .figure {{
      margin-top: 14px;
      border: 1px solid var(--stroke);
      border-radius: var(--radius);
      overflow: hidden;
      background: rgba(255,255,255,0.03);
    }}
    .figure img {{
      width: 100%;
      display: block;
    }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 520px) {{
      .grid {{ grid-template-columns: 1fr; }}
      header {{ flex-direction: column; align-items: flex-start; }}
      .meta {{ white-space: normal; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>{html_escape(title)}</h1>
      <div class="meta">Generated: {html_escape(ts)}</div>
    </header>

    <div class="grid">
      {card("Max total steps (ALL)", f'{_safe(summary["max_total_steps_all_stages"]["problem_id"])} • {_safe(summary["max_total_steps_all_stages"]["steps"])}')}
      {card("Avg steps (Localization, ALL)", f'{summary["avg_steps_localization"]:.2f}')}
      {card("Avg steps (Mitigation 0, ALL)", f'{summary["avg_steps_mitigation_0"]:.2f}')}
      {card("Min total steps (ALL)", f'{_safe(summary["min_total_steps_all_stages"]["problem_id"])} • {_safe(summary["min_total_steps_all_stages"]["steps"])}')}
    </div>

    <div class="panel">
      <h2>Summary metrics</h2>
      <table>
        <thead>
          <tr>
            <th style="width: 12%;">Mode</th>
            <th style="width: 38%;">Metric</th>
            <th style="width: 34%;">Item</th>
            <th style="width: 16%;">Value</th>
          </tr>
        </thead>
        <tbody>
          {''.join(
              f"<tr><td>{html_escape(str(mode))}</td><td>{html_escape(str(metric))}</td><td>{html_escape(str(item))}</td><td>{html_escape(str(val))}</td></tr>"
              for mode, metric, item, val in table_rows
          )}
        </tbody>
      </table>

      <div class="figure">
        {f'<img alt="Tool usage figure" src="data:image/png;base64,{img_b64}"/>' if img_b64 else '<div style="padding:14px;color:var(--muted);">Figure not available.</div>'}
      </div>
    </div>
  </div>
</body>
</html>
"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nWrote HTML report: {out_path}")



summary = collect_summary(problem_dfs)
pretty_print_summary(summary)

tool_calls_per_step = step_to_tool_call(problem_dfs)
fig_path = "tool_usage_by_step.png"


write_html_report(summary, fig_path=fig_path, out_path="analysis_report.html", title="Stratus Evaluation Report")
plot_tool_usage_by_step(
    tool_calls_per_step,
    top_k_tools=10,
    gap=6.0,
    width=None,
    title="Top-10 tool usage by step",
    save_path="tool_usage_by_step_.png",
    show=False,
)
print("\nDone.")
