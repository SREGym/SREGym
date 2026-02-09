import argparse
import base64
import json
import os
import warnings
from collections import Counter, defaultdict
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Dict, Tuple, Optional, List

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd



def pick_results_csv_with_most_rows(root: Path) -> Path:
    """
    Pick the *results.csv file with the most data rows* (excluding header).
    Searches under `root` (including subfolders).
    """
    candidates = list(root.rglob("*results.csv"))
    if not candidates:
        raise FileNotFoundError(f"No '*results.csv' found under {root}")

    best_path = None
    best_rows = -1

    for p in candidates:
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as f:
                n_lines = sum(1 for _ in f)
            n_rows = max(0, n_lines - 1)
        except Exception:
            continue

        if n_rows > best_rows:
            best_rows = n_rows
            best_path = p

    if best_path is None:
        raise FileNotFoundError(f"Found '*results.csv' under {root}, but none were readable.")

    print(f"[results.csv] Using: {best_path}  (rows={best_rows})")
    return best_path


def _coerce_bool_series(s: pd.Series) -> pd.Series:
    """
    Robust bool coercion for columns that might be:
      - bool
      - 0/1
      - "True"/"False"
      - NaN
    """
    if s.dtype == bool:
        return s.fillna(False)

    def to_bool(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, np.integer)):
            return bool(v)
        if isinstance(v, str):
            t = v.strip().lower()
            if t in {"true", "t", "1", "yes", "y"}:
                return True
            if t in {"false", "f", "0", "no", "n", ""}:
                return False
        # Fallback: safest false-ish default
        return bool(v)

    return s.map(to_bool).astype(bool)


# ----------------------------
# Data loading
# ----------------------------
def load_results_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    pd.set_option("display.max_columns", None)

    # Normalize success columns if present
    if "Diagnosis.success" in df.columns:
        df["Diagnosis.success"] = _coerce_bool_series(df["Diagnosis.success"])
    if "Mitigation.success" in df.columns:
        df["Mitigation.success"] = _coerce_bool_series(df["Mitigation.success"])

    return df


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


def build_jsonl_index(traces_root: Path) -> Dict[str, Path]:
    """
    Build problem_id -> jsonl_path index by reading the first JSON object of each file.
    If multiple JSONLs claim the same problem_id, we keep the largest file (usually most complete).
    """
    idx: Dict[str, Path] = {}
    sizes: Dict[str, int] = {}

    for p in traces_root.rglob("*.jsonl"):
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        pid = obj.get("problem_id")
                        if isinstance(pid, str) and pid.strip():
                            pid = pid.strip()
                            sz = p.stat().st_size
                            if pid not in idx or sz > sizes.get(pid, -1):
                                idx[pid] = p
                                sizes[pid] = sz
                    break
        except Exception:
            continue

    print(f"[jsonl] Indexed {len(idx)} problem_id(s) under {traces_root}")
    return idx


def load_jsonl_into_df(problem_id: str, jsonl_index: Dict[str, Path]) -> pd.DataFrame:
    rows = []
    jsonl_path = jsonl_index.get(problem_id)
    if not jsonl_path:
        raise FileNotFoundError(f"No JSONL found for problem_id={problem_id}")

    with jsonl_path.open("r", encoding="utf-8", errors="ignore") as file:
        first_obj = None
        for line in file:
            line = line.strip()
            if not line:
                continue
            first_obj = json.loads(line)
            break

        if not isinstance(first_obj, dict):
            raise ValueError(f"First JSON record in {jsonl_path} is not a dict")

        problem = first_obj.get("problem_id", problem_id)

        # Continue from current file position
        for line in file:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)

            stage = obj.get("stage", "")
            num_steps = obj.get("num_steps", 0)
            messages = obj.get("messages", [])

            if not isinstance(messages, list):
                continue

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
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

    return pd.DataFrame(rows)


def build_problem_dfs(all_results_csv: pd.DataFrame, traces_root: Path) -> dict:
    problem_ids = all_results_csv["problem_id"].dropna().unique()
    jsonl_index = build_jsonl_index(traces_root)

    problem_dfs = {}
    for pid in problem_ids:
        try:
            problem_dfs[pid] = load_jsonl_into_df(pid, jsonl_index)
        except Exception as e:
            print(f"Error loading problem_id {pid}: {e}")
            continue
    return problem_dfs



def successful(all_results_csv: pd.DataFrame, problem_id, stage=None) -> bool:
    row = all_results_csv.loc[all_results_csv["problem_id"] == problem_id].iloc[0]

    if stage is None:
        return bool(row["Mitigation.success"]) and bool(row["Diagnosis.success"])

    col = "Diagnosis.success" if stage == "diagnosis" else "Mitigation.success"
    return bool(row[col])


def not_successful(all_results_csv: pd.DataFrame, problem_id, stage=None) -> bool:
    row = all_results_csv.loc[all_results_csv["problem_id"] == problem_id].iloc[0]

    if stage is None:
        return (not bool(row["Mitigation.success"])) or (not bool(row["Diagnosis.success"]))

    col = "Diagnosis.success" if stage == "diagnosis" else "Mitigation.success"
    return not bool(row[col])


def _passes_filter(all_results_csv: pd.DataFrame, problem_id, stage, filter_mode) -> bool:
    """
    filter_mode:
      - None: no filtering
      - "success": only successful for that stage (or overall if stage=None)
      - "fail": only not-successful for that stage (or overall if stage=None)
    """
    if filter_mode is None:
        return True
    if filter_mode == "success":
        return successful(all_results_csv, problem_id, stage)
    if filter_mode == "fail":
        return not_successful(all_results_csv, problem_id, stage)
    raise ValueError("filter_mode must be None, 'success', or 'fail'")


# ----------------------------
# Tool-call aggregation
# ----------------------------
def iter_step_tool_calls(stage_df: pd.DataFrame):
    """
    Yields (step_num, merged_tool_calls_list)
    De-duplicates repeated rows with the same num_steps by aggregating tool_calls
    across those rows once per unique num_steps.
    """
    if stage_df.empty:
        return
    for step, grp in stage_df.groupby("num_steps", dropna=True, sort=False):
        merged = []
        for cell in grp["tool_calls"]:
            if isinstance(cell, list) and cell:
                merged.extend(cell)
        yield step, merged


# ----------------------------
# Metrics: steps
# ----------------------------
def problem_with_max_steps(all_results_csv, problem_dfs, stage, filter_mode=None):
    max_steps = -1
    max_problem_id = None
    if not stage:
        return None, -1

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        steps = stage_df["num_steps"].max()
        if pd.isna(steps):
            continue
        if steps > max_steps:
            max_steps = steps
            max_problem_id = problem_id

    return max_problem_id, max_steps


def problem_with_min_steps(all_results_csv, problem_dfs, stage, filter_mode=None):
    min_steps = float("inf")
    min_problem_id = None
    if not stage:
        return None, float("inf")

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        steps = stage_df["num_steps"].max()
        if pd.isna(steps):
            continue
        if steps < min_steps:
            min_steps = steps
            min_problem_id = problem_id

    return min_problem_id, min_steps


def total_maximum_steps(all_results_csv, problem_dfs, filter_mode=None):
    if not problem_dfs:
        return None, 0, {}

    stages = problem_dfs[next(iter(problem_dfs))]["stage"].dropna().unique()
    problem_id_to_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage=None, filter_mode=filter_mode):
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


def total_minimum_steps(all_results_csv, problem_dfs, filter_mode=None):
    if not problem_dfs:
        return None, 0, {}

    stages = problem_dfs[next(iter(problem_dfs))]["stage"].dropna().unique()
    problem_id_to_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage=None, filter_mode=filter_mode):
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


def avg_steps_per_stage(all_results_csv, problem_dfs, stage, filter_mode=None):
    total_steps = 0
    count = 0
    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        steps = stage_df["num_steps"].max()
        if not pd.isna(steps):
            total_steps += int(steps)
            count += 1
    return total_steps / count if count > 0 else 0


# ----------------------------
# Metrics: tool frequencies
# ----------------------------
def most_frequently_used_tool(all_results_csv, problem_dfs, stage, filter_mode=None):
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        for _, tool_calls in iter_step_tool_calls(stage_df):
            tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
            for name in tools_in_step:
                tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    most_used_tool = max(tool_count, key=tool_count.get)
    return most_used_tool, tool_count[most_used_tool]


def least_frequently_used_tool(all_results_csv, problem_dfs, stage, filter_mode=None):
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage, filter_mode):
            continue

        stage_df = df[df["stage"] == stage]
        for _, tool_calls in iter_step_tool_calls(stage_df):
            tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
            for name in tools_in_step:
                tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    least_used_tool = min(tool_count, key=tool_count.get)
    return least_used_tool, tool_count[least_used_tool]


def total_most_frequently_used_tool(all_results_csv, problem_dfs, filter_mode=None):
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage=None, filter_mode=filter_mode):
            continue

        for stg in df["stage"].dropna().unique():
            stage_df = df[df["stage"] == stg]
            for _, tool_calls in iter_step_tool_calls(stage_df):
                tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
                for name in tools_in_step:
                    tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    most_used_tool = max(tool_count, key=tool_count.get)
    return most_used_tool, tool_count[most_used_tool]


def total_least_frequently_used_tool(all_results_csv, problem_dfs, filter_mode=None):
    tool_count = {}

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage=None, filter_mode=filter_mode):
            continue

        for stg in df["stage"].dropna().unique():
            stage_df = df[df["stage"] == stg]
            for _, tool_calls in iter_step_tool_calls(stage_df):
                tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
                for name in tools_in_step:
                    tool_count[name] = tool_count.get(name, 0) + 1

    if not tool_count:
        return None, 0

    least_used_tool = min(tool_count, key=tool_count.get)
    return least_used_tool, tool_count[least_used_tool]


def step_to_tool_call(all_results_csv, problem_dfs, filter_mode=None):
    tool_count_per_step = defaultdict(Counter)

    for problem_id, df in problem_dfs.items():
        if not _passes_filter(all_results_csv, problem_id, stage=None, filter_mode=filter_mode):
            continue

        for stg in df["stage"].dropna().unique():
            stage_df = df[df["stage"] == stg]
            for step, tool_calls in iter_step_tool_calls(stage_df):
                tools_in_step = {tc.get("name") for tc in tool_calls if tc.get("name")}
                for name in tools_in_step:
                    tool_count_per_step[int(step)][name] += 1

    print(all_results_csv[["Diagnosis.success", "Mitigation.success"]].dtypes)
    print("Diagnosis unique:", all_results_csv["Diagnosis.success"].unique()[:10])
    print("Mitigation unique:", all_results_csv["Mitigation.success"].unique()[:10])

    return dict(tool_count_per_step)


# ----------------------------
# Correlations (robust / no warnings)
# ----------------------------
def _safe_corr(x: pd.Series, y: pd.Series, method: str) -> Tuple[Optional[float], Optional[str]]:
    df = pd.DataFrame({"x": x, "y": y}).dropna()
    if df.empty or df.shape[0] < 2:
        return None, "N/A (insufficient data)"
    if df["x"].nunique() < 2:
        return None, "N/A (x is constant)"
    if df["y"].nunique() < 2:
        return None, "N/A (y is constant)"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        val = df["x"].corr(df["y"], method=method)

    if val != val:
        return None, "N/A (corr undefined)"
    return float(val), None


def correlation_tool_calls_vs_success(
    all_results_csv: pd.DataFrame,
    problem_dfs: dict,
    tool_metric: str = "tool_calls_total",
    filter_mode: str | None = None,
    n_bins: int = 5,
):
    valid = {"tool_calls_total", "tool_steps_with_any_tool", "distinct_tools"}
    if tool_metric not in valid:
        raise ValueError(f"tool_metric must be one of {sorted(valid)}")

    csv_pids = set(all_results_csv["problem_id"])
    rows = []

    for pid, df in problem_dfs.items():
        if pid not in csv_pids:
            continue
        if not _passes_filter(all_results_csv, pid, stage=None, filter_mode=filter_mode):
            continue

        raw_total = 0
        for cell in df.get("tool_calls", []):
            if isinstance(cell, list):
                raw_total += len(cell)

        steps_with_any_tool = 0
        tool_names = set()
        for stg in df["stage"].dropna().unique():
            stage_df = df[df["stage"] == stg]
            for _, tool_calls in iter_step_tool_calls(stage_df):
                names = {tc.get("name") for tc in tool_calls if isinstance(tc, dict) and tc.get("name")}
                if names:
                    steps_with_any_tool += 1
                    tool_names |= names

        r = all_results_csv.loc[all_results_csv["problem_id"] == pid].iloc[0]
        diag_ok = bool(r["Diagnosis.success"])
        mit_ok = bool(r["Mitigation.success"])
        overall_ok = diag_ok and mit_ok

        rows.append({
            "problem_id": pid,
            "tool_calls_total": int(raw_total),
            "tool_steps_with_any_tool": int(steps_with_any_tool),
            "distinct_tools": int(len(tool_names)),
            "diagnosis_success": diag_ok,
            "mitigation_success": mit_ok,
            "overall_success": overall_ok,
        })

    feat = pd.DataFrame(rows)
    if feat.empty:
        return {
            "features_df": feat,
            "correlations": {},
            "balance": {},
            "bins": {},
            "tool_metric": tool_metric,
            "filter_mode": filter_mode,
        }

    x = feat[tool_metric].astype(float)

    def _balance(col: str) -> Dict[str, int]:
        s = feat[col].astype(bool)
        return {"true": int(s.sum()), "false": int((~s).sum()), "n": int(len(s))}

    corrs = {}
    balance = {}
    for tgt in ["diagnosis_success", "mitigation_success", "overall_success"]:
        balance[tgt] = _balance(tgt)

        pear, pear_reason = _safe_corr(x, feat[tgt].astype(int), method="pearson")
        spear, spear_reason = _safe_corr(x, feat[tgt].astype(int), method="spearman")

        corrs[tgt] = {
            "pearson": pear,
            "pearson_reason": pear_reason,
            "spearman": spear,
            "spearman_reason": spear_reason,
        }

    bins = {}
    try:
        if feat[tool_metric].nunique() >= 2:
            q = pd.qcut(feat[tool_metric], q=n_bins, duplicates="drop")
            feat["_bin"] = q
            for tgt in ["diagnosis_success", "mitigation_success", "overall_success"]:
                bins[tgt] = (
                    feat.groupby("_bin", observed=True)
                    .agg(
                        n=("problem_id", "size"),
                        avg_tool_metric=(tool_metric, "mean"),
                        success_rate=(tgt, "mean"),
                    )
                    .sort_values("avg_tool_metric")
                )
    except Exception:
        bins = {}

    return {
        "features_df": feat.drop(columns=[c for c in ["_bin"] if c in feat.columns]),
        "correlations": corrs,
        "balance": balance,
        "bins": bins,
        "tool_metric": tool_metric,
        "filter_mode": filter_mode,
    }


def steps_tool_usage_correlation(
    all_results_csv: pd.DataFrame,
    problem_dfs: dict,
    filter_mode: str | None = None,
    tool_metric: str = "tool_calls_total",
):
    rows = []
    csv_pids = set(all_results_csv["problem_id"])

    for pid, df in problem_dfs.items():
        if pid not in csv_pids:
            continue
        if not _passes_filter(all_results_csv, pid, stage=None, filter_mode=filter_mode):
            continue

        total_steps = 0
        for stg in df["stage"].dropna().unique():
            mx = df.loc[df["stage"] == stg, "num_steps"].max()
            total_steps += 0 if pd.isna(mx) else int(mx)

        if tool_metric == "tool_calls_total":
            tool_val = 0
            for cell in df.get("tool_calls", []):
                if isinstance(cell, list):
                    tool_val += len(cell)
        elif tool_metric == "tool_steps_with_any_tool":
            tool_val = 0
            for stg in df["stage"].dropna().unique():
                stage_df = df[df["stage"] == stg]
                for _, tool_calls in iter_step_tool_calls(stage_df):
                    names = {tc.get("name") for tc in tool_calls if isinstance(tc, dict) and tc.get("name")}
                    if names:
                        tool_val += 1
        else:
            raise ValueError("tool_metric must be 'tool_calls_total' or 'tool_steps_with_any_tool'")

        rows.append({"problem_id": pid, "total_steps": total_steps, "tool_usage": tool_val})

    corr_df = pd.DataFrame(rows)
    label = f"pearson(total_steps vs {tool_metric})"
    if corr_df.empty:
        return label, "N/A (no data)"
    if corr_df["total_steps"].nunique() < 2:
        return label, "N/A (total_steps constant)"
    if corr_df["tool_usage"].nunique() < 2:
        return label, "N/A (tool_usage constant)"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pearson = corr_df["total_steps"].corr(corr_df["tool_usage"], method="pearson")

    if pearson != pearson:
        return label, "N/A (corr undefined)"
    return label, float(pearson)


#plotting
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
    if not tool_count_per_step:
        print("No tool counts to plot.")
        return None, None

    steps = sorted(tool_count_per_step.keys())
    n = len(steps)

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

    x = np.arange(n) * gap
    if width is None:
        width = 0.72 * gap

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


# ----------------------------
# Reporting (terminal + HTML)
# ----------------------------
def _safe(v):
    return "" if v is None else v


def _mode_label(mode: str) -> str:
    return {"all": "ALL", "success": "SUCCESS", "fail": "FAIL"}[mode]


def _mode_suffix(mode: str) -> str:
    return "" if mode == "all" else f"_{mode}"


def _fmt_corr(val: Optional[float], reason: Optional[str]) -> str:
    if val is None:
        return reason or "N/A"
    return f"{val:.3f}"


def collect_summary(all_results_csv: pd.DataFrame, problem_dfs: dict) -> dict:
    summary = {}
    modes = ["all", "success", "fail"]

    for mode in modes:
        filter_mode = None if mode == "all" else mode
        suf = _mode_suffix(mode)

        pid, steps = problem_with_max_steps(all_results_csv, problem_dfs, stage="diagnosis", filter_mode=filter_mode)
        summary[f"max_steps_diagnosis{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps = problem_with_max_steps(all_results_csv, problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"max_steps_mitigation_0{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps, _ = total_maximum_steps(all_results_csv, problem_dfs, filter_mode=filter_mode)
        summary[f"max_total_steps_all_stages{suf}"] = {"problem_id": pid, "steps": steps}

        summary[f"avg_steps_diagnosis{suf}"] = avg_steps_per_stage(all_results_csv, problem_dfs, stage="diagnosis", filter_mode=filter_mode)
        summary[f"avg_steps_mitigation_0{suf}"] = avg_steps_per_stage(all_results_csv, problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)

        pid, steps = problem_with_min_steps(all_results_csv, problem_dfs, stage="diagnosis", filter_mode=filter_mode)
        summary[f"min_steps_diagnosis{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps = problem_with_min_steps(all_results_csv, problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"min_steps_mitigation_0{suf}"] = {"problem_id": pid, "steps": steps}

        pid, steps, _ = total_minimum_steps(all_results_csv, problem_dfs, filter_mode=filter_mode)
        summary[f"min_total_steps_all_stages{suf}"] = {"problem_id": pid, "steps": steps}

        tool, c = most_frequently_used_tool(all_results_csv, problem_dfs, stage="diagnosis", filter_mode=filter_mode)
        summary[f"most_used_tool_diagnosis{suf}"] = {"tool": tool, "steps": c}

        tool, c = most_frequently_used_tool(all_results_csv, problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"most_used_tool_mitigation_0{suf}"] = {"tool": tool, "steps": c}

        tool, c = total_most_frequently_used_tool(all_results_csv, problem_dfs, filter_mode=filter_mode)
        summary[f"most_used_tool_all_stages{suf}"] = {"tool": tool, "steps": c}

        tool, c = least_frequently_used_tool(all_results_csv, problem_dfs, stage="diagnosis", filter_mode=filter_mode)
        summary[f"least_used_tool_diagnosis{suf}"] = {"tool": tool, "steps": c}

        tool, c = least_frequently_used_tool(all_results_csv, problem_dfs, stage="mitigation_attempt_0", filter_mode=filter_mode)
        summary[f"least_used_tool_mitigation_0{suf}"] = {"tool": tool, "steps": c}

        tool, c = total_least_frequently_used_tool(all_results_csv, problem_dfs, filter_mode=filter_mode)
        summary[f"least_used_tool_all_stages{suf}"] = {"tool": tool, "steps": c}

        corr = correlation_tool_calls_vs_success(
            all_results_csv,
            problem_dfs,
            tool_metric="tool_calls_total",
            filter_mode=filter_mode,
            n_bins=5,
        )

        if mode == "all":
            summary["balance_diagnosis"] = corr["balance"].get("diagnosis_success", {})
            summary["balance_mitigation"] = corr["balance"].get("mitigation_success", {})
            summary["balance_overall"] = corr["balance"].get("overall_success", {})

        for tgt, key in [
            ("diagnosis_success", "diag"),
            ("mitigation_success", "mit"),
            ("overall_success", "overall"),
        ]:
            pear = corr.get("correlations", {}).get(tgt, {}).get("pearson")
            pear_reason = corr.get("correlations", {}).get(tgt, {}).get("pearson_reason")
            summary[f"corr_toolcalls_vs_{key}{suf}"] = _fmt_corr(pear, pear_reason)

        label, val = steps_tool_usage_correlation(
            all_results_csv,
            problem_dfs,
            filter_mode=filter_mode,
            tool_metric="tool_calls_total",
        )
        summary[f"steps_tool_usage_label{suf}"] = label
        summary[f"steps_tool_usage_value{suf}"] = val

    return summary


def pretty_print_summary(summary: dict):
    modes = ["all", "success", "fail"]
    rows = []

    for mode in modes:
        suf = _mode_suffix(mode)
        m = _mode_label(mode)

        rows.extend([
            (m, "Max steps (diagnosis)",
             summary[f"max_steps_diagnosis{suf}"]["problem_id"],
             summary[f"max_steps_diagnosis{suf}"]["steps"]),

            (m, "Max steps (Mitigation 0)",
             summary[f"max_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"max_steps_mitigation_0{suf}"]["steps"]),

            (m, "Max total steps (All stages)",
             summary[f"max_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"max_total_steps_all_stages{suf}"]["steps"]),

            (m, "Avg steps (diagnosis)", "-", f'{summary[f"avg_steps_diagnosis{suf}"]:.2f}'),
            (m, "Avg steps (Mitigation 0)", "-", f'{summary[f"avg_steps_mitigation_0{suf}"]:.2f}'),

            (m, "Min steps (diagnosis)",
             summary[f"min_steps_diagnosis{suf}"]["problem_id"],
             summary[f"min_steps_diagnosis{suf}"]["steps"]),

            (m, "Min steps (Mitigation 0)",
             summary[f"min_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"min_steps_mitigation_0{suf}"]["steps"]),

            (m, "Min total steps (All stages)",
             summary[f"min_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"min_total_steps_all_stages{suf}"]["steps"]),

            (m, "Most used tool (diagnosis)",
             summary[f"most_used_tool_diagnosis{suf}"]["tool"],
             summary[f"most_used_tool_diagnosis{suf}"]["steps"]),

            (m, "Most used tool (Mitigation 0)",
             summary[f"most_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"most_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Most used tool (All stages)",
             summary[f"most_used_tool_all_stages{suf}"]["tool"],
             summary[f"most_used_tool_all_stages{suf}"]["steps"]),

            (m, "Least used tool (diagnosis)",
             summary[f"least_used_tool_diagnosis{suf}"]["tool"],
             summary[f"least_used_tool_diagnosis{suf}"]["steps"]),

            (m, "Least used tool (Mitigation 0)",
             summary[f"least_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"least_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Least used tool (All stages)",
             summary[f"least_used_tool_all_stages{suf}"]["tool"],
             summary[f"least_used_tool_all_stages{suf}"]["steps"]),

            (m, "Corr(tool_calls_total, Diagnosis.success) [Pearson]", "-", summary.get(f"corr_toolcalls_vs_diag{suf}", "-")),
            (m, "Corr(tool_calls_total, Mitigation.success) [Pearson]", "-", summary.get(f"corr_toolcalls_vs_mit{suf}", "-")),
            (m, "Corr(tool_calls_total, Overall.success) [Pearson]", "-", summary.get(f"corr_toolcalls_vs_overall{suf}", "-")),

            (m, "Correlation between total_steps and tool usage",
             summary.get(f"steps_tool_usage_label{suf}", "-"),
             summary.get(f"steps_tool_usage_value{suf}", "-")),
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
            (m, "Max steps (diagnosis)",
             summary[f"max_steps_diagnosis{suf}"]["problem_id"],
             summary[f"max_steps_diagnosis{suf}"]["steps"]),

            (m, "Max steps (Mitigation 0)",
             summary[f"max_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"max_steps_mitigation_0{suf}"]["steps"]),

            (m, "Max total steps (All stages)",
             summary[f"max_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"max_total_steps_all_stages{suf}"]["steps"]),

            (m, "Avg steps (diagnosis)", "-", f'{summary[f"avg_steps_diagnosis{suf}"]:.2f}'),
            (m, "Avg steps (Mitigation 0)", "-", f'{summary[f"avg_steps_mitigation_0{suf}"]:.2f}'),

            (m, "Min steps (diagnosis)",
             summary[f"min_steps_diagnosis{suf}"]["problem_id"],
             summary[f"min_steps_diagnosis{suf}"]["steps"]),

            (m, "Min steps (Mitigation 0)",
             summary[f"min_steps_mitigation_0{suf}"]["problem_id"],
             summary[f"min_steps_mitigation_0{suf}"]["steps"]),

            (m, "Min total steps (All stages)",
             summary[f"min_total_steps_all_stages{suf}"]["problem_id"],
             summary[f"min_total_steps_all_stages{suf}"]["steps"]),

            (m, "Most used tool (diagnosis)",
             summary[f"most_used_tool_diagnosis{suf}"]["tool"],
             summary[f"most_used_tool_diagnosis{suf}"]["steps"]),

            (m, "Most used tool (Mitigation 0)",
             summary[f"most_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"most_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Most used tool (All stages)",
             summary[f"most_used_tool_all_stages{suf}"]["tool"],
             summary[f"most_used_tool_all_stages{suf}"]["steps"]),

            (m, "Least used tool (diagnosis)",
             summary[f"least_used_tool_diagnosis{suf}"]["tool"],
             summary[f"least_used_tool_diagnosis{suf}"]["steps"]),

            (m, "Least used tool (Mitigation 0)",
             summary[f"least_used_tool_mitigation_0{suf}"]["tool"],
             summary[f"least_used_tool_mitigation_0{suf}"]["steps"]),

            (m, "Least used tool (All stages)",
             summary[f"least_used_tool_all_stages{suf}"]["tool"],
             summary[f"least_used_tool_all_stages{suf}"]["steps"]),

            (m, "Corr(tool_calls_total, Diagnosis.success) [Pearson]", "-", summary.get(f"corr_toolcalls_vs_diag{suf}", "-")),
            (m, "Corr(tool_calls_total, Mitigation.success) [Pearson]", "-", summary.get(f"corr_toolcalls_vs_mit{suf}", "-")),
            (m, "Corr(tool_calls_total, Overall.success) [Pearson]", "-", summary.get(f"corr_toolcalls_vs_overall{suf}", "-")),

            (m, "Correlation between total_steps and tool usage",
             summary.get(f"steps_tool_usage_label{suf}", "-"),
             summary.get(f"steps_tool_usage_value{suf}", "-")),
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
      {card("Avg steps (diagnosis, ALL)", f'{summary["avg_steps_diagnosis"]:.2f}')}
      {card("Avg steps (Mitigation 0, ALL)", f'{summary["avg_steps_mitigation_0"]:.2f}')}
      {card("Min total steps (ALL)", f'{_safe(summary["min_total_steps_all_stages"]["problem_id"])} • {_safe(summary["min_total_steps_all_stages"]["steps"])}')}
    </div>

    <div class="panel">
      <h2>Summary metrics</h2>
      <table>
        <thead>
          <tr>
            <th style="width: 12%;">Mode</th>
            <th style="width: 42%;">Metric</th>
            <th style="width: 30%;">Item</th>
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



def main():
    ap = argparse.ArgumentParser(description="Analyze Stratus results + traces and produce terminal + HTML report.")
    ap.add_argument(
        "results_root",
        nargs="?",
        default=".",
        help="Directory containing traces (*.jsonl). If --csv not set, we also search here for *results.csv.",
    )
    ap.add_argument(
        "--csv",
        default=None,
        help="Path to results.csv. If omitted, we pick the *results.csv under results_root with the most rows.",
    )
    ap.add_argument("-o", "--out", default="analysis_report.html", help="Output HTML report path.")
    ap.add_argument("--fig", default="tool_usage_by_step.png", help="Output plot path.")
    args = ap.parse_args()

    results_root = Path(args.results_root).expanduser().resolve()

    if args.csv:
        csv_path = Path(args.csv).expanduser().resolve()
    else:
        csv_path = pick_results_csv_with_most_rows(results_root)

    all_results_csv = load_results_csv(csv_path)

    problem_dfs = build_problem_dfs(all_results_csv, traces_root=results_root)

    summary = collect_summary(all_results_csv, problem_dfs)
    pretty_print_summary(summary)

    tool_calls_per_step = step_to_tool_call(all_results_csv, problem_dfs, filter_mode=None)

    plot_tool_usage_by_step(
        tool_calls_per_step,
        top_k_tools=10,
        gap=6.0,
        width=None,
        title="Top-10 tool usage by step",
        save_path=args.fig,
        show=False,
    )

    write_html_report(summary, fig_path=args.fig, out_path=args.out, title="Stratus Evaluation Report")
    print("\nDone.")


if __name__ == "__main__":
    main()
