import argparse
import csv
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd


# Keep ONLY the single highest-event_index "event" record per stage (per file),
# but render the FULL event using your existing HTML logic.
@dataclass(frozen=True)
class Tags:
    namespace: str
    application: str
    diagnosis_success: bool
    mitigation_success: bool
    resolution_success: bool
    overall_success: bool


TARGET_STAGES_ORDER = ["diagnosis", "mitigation_attempt_0"]
# Regex to match any mitigation retry stage: mitigation_attempt_0, mitigation_attempt_1, ...
_MITIGATION_ATTEMPT_RE = re.compile(r"^mitigation_attempt_\d+$")
all_results_csv: pd.DataFrame | None = None
ATTR_INDEX: dict[str, dict[str, Any]] = {}
tags_by_problem_id = {}


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


HOT_KEYS = {
    "type",
    "problem_id",
    "timestamp",
    "timestamp_readable",
    "total_stages",
    "total_events",
    "stage",
    "event_index",
    "num_steps",
    "submitted",
    "rollback_stack",
    "last_message",
    "messages",
}


def _csv_row(problem_id: str) -> pd.Series:
    if all_results_csv is None:
        raise RuntimeError("all_results_csv not initialized. Did you call main() correctly?")
    return all_results_csv.loc[all_results_csv["problem_id"] == problem_id].iloc[0]


def _as_bool(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() in {"1", "true", "yes"}
    if isinstance(x, (int, float)):
        return x != 0
    return False


def diagnosis_success(problem_id: str) -> bool:
    row = _csv_row(problem_id)
    return _as_bool(row.get("Diagnosis.success"))


def mitigation_success(problem_id: str) -> bool:
    row = _csv_row(problem_id)
    return _as_bool(row.get("Mitigation.success"))


def resolution_success(problem_id: str) -> bool:
    row = _csv_row(problem_id)
    return _as_bool(row.get("Resolution.success"))


def overall_success(problem_id: str) -> bool:
    return diagnosis_success(problem_id) and mitigation_success(problem_id)


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name.strip())
    return name[:180] if name else "report"


_MITIGATION_ATTEMPT_RE = re.compile(r"^mitigation_attempt_(\d+)$")
_RUN_LABEL_RE = re.compile(r"^run_\d+$")


def discover_stages(path: Path) -> list[str]:
    """
    Stream a JSONL file and return all stage names found, ordered as:
      diagnosis → mitigation_attempt_0 → mitigation_attempt_1 → … → other stages
    This replaces the hardcoded TARGET_STAGES_ORDER so every attempt is rendered.
    """
    found: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            stage = obj.get("stage")
            if isinstance(stage, str) and stage:
                found.add(stage)

    attempts = sorted(
        (s for s in found if _MITIGATION_ATTEMPT_RE.match(s)),
        key=lambda s: int(_MITIGATION_ATTEMPT_RE.match(s).group(1)),
    )
    base = ["diagnosis"] if "diagnosis" in found else []
    other = sorted(s for s in found if s != "diagnosis" and s not in attempts)
    return base + attempts + other


# ---------------------------------------------------------------------------
# Tool-call CSV export
# ---------------------------------------------------------------------------

def _extract_tool_calls_from_msg(msg: dict) -> list[dict]:
    """Extract tool calls from a message, handling all known formats."""
    # OpenAI / LangChain direct format
    tcs = msg.get("tool_calls", [])
    if isinstance(tcs, list) and tcs:
        result = []
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name")
            args = tc.get("args")
            if not name:
                fn = tc.get("function", {})
                if isinstance(fn, dict):
                    name = fn.get("name")
                    args = fn.get("arguments")
            if name:
                result.append({"name": name, "args": args})
        if result:
            return result

    # LangChain additional_kwargs format
    ak = msg.get("additional_kwargs", {})
    if isinstance(ak, dict):
        tcs2 = ak.get("tool_calls", [])
        if isinstance(tcs2, list) and tcs2:
            result = []
            for tc in tcs2:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name")
                args = tc.get("args")
                if not name:
                    fn = tc.get("function", {})
                    if isinstance(fn, dict):
                        name = fn.get("name")
                        args = fn.get("arguments")
                if name:
                    result.append({"name": name, "args": args})
            if result:
                return result

    # Anthropic content-block format (tool_use blocks inside content list)
    content = msg.get("content")
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                args = block.get("input", {})
                if name:
                    result.append({"name": name, "args": args})
        if result:
            return result

    return []


_KUBECTL_TOOL_NAMES = {"exec_kubectl_cmd_safely", "exec_read_only_kubectl_cmd"}


def _format_tool_call_signature(tc: dict) -> str:
    """Format a tool call as name(arg1=val1, arg2=val2).

    For kubectl tools, returns just the raw kubectl command string.
    """
    name = tc.get("name", "unknown")
    args = tc.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            pass

    # For kubectl tools, return the raw command starting with "kubectl"
    if name in _KUBECTL_TOOL_NAMES and isinstance(args, dict):
        cmd = args.get("command", "")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()

    if isinstance(args, dict):
        parts = [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items()]
        return f"{name}({', '.join(parts)})"
    return f"{name}()"


def save_tool_calls_csv(
    records: list[dict[str, Any]],
    out_dir: Path,
    prefix: str,
) -> None:
    """
    Save executed tool calls to CSV files with columns: turn_index, tool_call.

    For stratus (diagnosis + mitigation stages), separate CSV files are written
    for the diagnosis agent and the mitigation agent.  For single-stage agents,
    one CSV is written.

    Each assistant turn that contains tool calls gets a sequential *turn_index*
    (0-based, per stage).  Each individual tool call within that turn is a
    separate row sharing the same turn_index.
    """
    # stage -> list of (turn_index, formatted_signature)
    stage_tool_calls: dict[str, list[tuple[int, str]]] = {}

    for rec in records:
        stage = rec.get("stage", "unknown")
        msgs = detect_messages(rec)
        if not msgs:
            continue

        turn_index = 0
        for msg in msgs:
            tcs = _extract_tool_calls_from_msg(msg)
            if tcs:
                for tc in tcs:
                    sig = _format_tool_call_signature(tc)
                    stage_tool_calls.setdefault(stage, []).append((turn_index, sig))
                turn_index += 1

    if not stage_tool_calls:
        return

    stages = list(stage_tool_calls.keys())
    has_diagnosis = "diagnosis" in stages
    has_mitigation = any(s.startswith("mitigation") for s in stages)
    is_multi_agent = has_diagnosis and has_mitigation

    if is_multi_agent:
        for stage, calls in stage_tool_calls.items():
            stage_label = "diagnosis" if stage == "diagnosis" else stage
            csv_path = out_dir / f"{prefix}{stage_label}_tool_calls.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["turn_index", "tool_call"])
                for turn_idx, sig in calls:
                    writer.writerow([turn_idx, sig])
    else:
        all_calls = []
        for calls in stage_tool_calls.values():
            all_calls.extend(calls)
        csv_path = out_dir / f"{prefix}tool_calls.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["turn_index", "tool_call"])
            for turn_idx, sig in all_calls:
                writer.writerow([turn_idx, sig])


def extract_run_label(path: Path) -> str:
    """
    Return the run_N segment from a file path (e.g. …/run_2/trajectory/foo.jsonl → 'run_2').
    Returns '' if no run_N segment is present.
    """
    for part in reversed(path.parts):
        if _RUN_LABEL_RE.match(part):
            return part
    return ""


def extract_agent_label(path: Path) -> str:
    """
    Extract the agent name from the directory structure.

    main.py writes runs to:  results/{timestamp}/{agent}/{problem_id}/run_{N}/…
    So the agent directory is always 2 levels above the run_N segment.

    Returns '' if the run_N segment cannot be found or there are fewer than
    2 parent directories above it.
    """
    parts = path.parts
    for i, part in enumerate(parts):
        if _RUN_LABEL_RE.match(part):
            agent_idx = i - 2
            if agent_idx >= 0:
                return parts[agent_idx]
            return ""
    return ""


def _to_int(x: Any) -> int | None:
    try:
        return int(x)
    except Exception:
        return None


def get_first(d: dict[str, Any], keys: list[str]) -> Any | None:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def nested_get(d: dict[str, Any], paths: list[list[str]]) -> Any | None:
    for path in paths:
        cur: Any = d
        ok = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def as_str(v: Any, max_len: int = 180) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float, bool)):
        s = str(v)
    elif isinstance(v, str):
        s = v
    else:
        s = json.dumps(v, ensure_ascii=False)
    s = s.replace("\n", " ").strip()
    return (s[: max_len - 1] + "…") if len(s) > max_len else s


def pretty_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def is_event_record(rec: dict[str, Any]) -> bool:
    return rec.get("type") == "event" and isinstance(rec.get("stage"), str) and ("event_index" in rec)


def detect_messages(rec: dict[str, Any]) -> list[dict[str, Any]] | None:
    """
    Your JSONL schema often has:
      - {"type":"event", ..., "messages":[{...}, ...], "last_message": {...}}
    """
    msgs = nested_get(rec, [["messages"], ["input", "messages"], ["output", "messages"]])
    if isinstance(msgs, list) and msgs and all(isinstance(m, dict) for m in msgs):
        return msgs
    return None


def detect_steps(rec: dict[str, Any]) -> list[dict[str, Any]] | None:
    steps = get_first(rec, ["steps", "events", "trace", "spans"])
    if isinstance(steps, list) and steps and all(isinstance(s, dict) for s in steps):
        return steps
    return None


def last_message_preview(rec: dict[str, Any], max_len: int = 160) -> str:
    """
    Prefer rec["last_message"], else fall back to the last item in messages.
    Returns: "<type>: <content-preview>"
    """
    lm = rec.get("last_message")
    if isinstance(lm, dict):
        t = as_str(lm.get("type") or lm.get("role") or "")
        c = lm.get("content")
        c_str = as_str(pretty_json(c), max_len=max_len) if isinstance(c, list) else as_str(c, max_len=max_len)
        out = f"{t}: {c_str}".strip(": ").strip()
        return out

    msgs = detect_messages(rec)
    if msgs:
        last = msgs[-1]
        t = as_str(last.get("type") or last.get("role") or "")
        c = last.get("content")
        c_str = as_str(pretty_json(c), max_len=max_len) if isinstance(c, list) else as_str(c, max_len=max_len)
        out = f"{t}: {c_str}".strip(": ").strip()
        return out

    return ""


def generate_analysis_report(root: Path) -> None:
    """
    Run queries.py *as if root were the working directory*.
    This lets queries.py use relative paths under that root without editing it.
    """
    directory = Path(__file__).resolve().parent
    path = directory / "queries.py"
    import os

    cwd = os.getcwd()
    subprocess.run(["python3", str(path), root, "-o analysis_report.html"], check=True, cwd=cwd)


def _stage_matches(stage: str, stages_order: list[str]) -> bool:
    """Check if a stage is in the explicit list or matches the mitigation_attempt_N pattern."""
    return stage in stages_order or _MITIGATION_ATTEMPT_RE.match(stage) is not None


def _mitigation_attempt_sort_key(stage: str) -> tuple[int, int]:
    """Sort key: (0, 0) for 'diagnosis', (1, N) for 'mitigation_attempt_N'."""
    m = _MITIGATION_ATTEMPT_RE.match(stage)
    if m:
        return (1, int(stage.rsplit("_", 1)[1]))
    # Non-mitigation stages come first, in their original order
    return (0, 0)


def stream_pick_highest_event_index_per_stage(
    path: Path,
    stages_order: list[str],
) -> tuple[list[dict[str, Any]], list[str], int]:
    """
    Stream the JSONL file; do NOT store all records.
    Keep ONLY the highest event_index event per target stage.
    Dynamically discovers all mitigation_attempt_N stages (not just _0).
    """
    errors: list[str] = []
    total_lines = 0

    # best_num[stage] = (event_index_int, line_no, record)
    best_num: dict[str, tuple[int, int, dict[str, Any]]] = {}
    # best_fallback[stage] = (line_no, record)  # used only if no numeric seen
    best_fallback: dict[str, tuple[int, dict[str, Any]]] = {}

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                errors.append(f"{path.name}:{line_no}: {e}")
                continue

            if not isinstance(obj, dict):
                continue
            if not is_event_record(obj):
                continue

            stage = obj.get("stage")
            if not _stage_matches(stage, stages_order):
                continue

            ei_int = _to_int(obj.get("event_index"))

            if ei_int is None:
                prev = best_fallback.get(stage)
                if prev is None or line_no > prev[0]:
                    best_fallback[stage] = (line_no, obj)
                continue

            prev = best_num.get(stage)
            if prev is None:
                best_num[stage] = (ei_int, line_no, obj)
            else:
                cur_ei, cur_ln, _ = prev
                if (ei_int > cur_ei) or (ei_int == cur_ei and line_no > cur_ln):
                    best_num[stage] = (ei_int, line_no, obj)

    # Build the final ordered list: explicit stages first, then any discovered
    # mitigation_attempt_N stages sorted numerically
    all_stages = set(best_num.keys()) | set(best_fallback.keys())
    ordered_stages = sorted(all_stages, key=_mitigation_attempt_sort_key)

    out: list[dict[str, Any]] = []
    for s in ordered_stages:
        if s in best_num:
            out.append(best_num[s][2])
        elif s in best_fallback:
            out.append(best_fallback[s][1])

    return out, errors, total_lines


def find_problem_id(path: Path) -> str:
    """
    Each JSONL file is one problem_id. If our selected records are empty,
    this finds the first dict with a non-empty problem_id by streaming.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    pid = obj.get("problem_id")
                    pid_s = as_str(pid)
                    if pid_s:
                        return pid_s
    except Exception:
        pass
    return ""


def load_problem_index(jsonl_path: str) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                problem_id = obj.get("problem_id", "")
                if problem_id and problem_id not in index:
                    index[problem_id] = obj
            except Exception:
                continue
    return index


def load_attributes_index(root: Path) -> dict[str, dict[str, Any]]:
    """
    Prefer attributes.jsonl under the provided root.
    Fallback to the script directory if not found.
    """
    candidates = [
        root / "attributes.jsonl",
        Path(__file__).resolve().parent / "attributes.jsonl",
    ]
    for p in candidates:
        if p.exists():
            try:
                return load_problem_index(str(p))
            except Exception:
                return {}
    return {}


FILTER_UI = """
<div class='card'>
  <h3 style='margin:0 0 10px 0;'>Filter</h3>
  <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
    <input id="q" placeholder="Search problem_id, type, origin, fault level..." style="padding:8px 10px; border:1px solid var(--border); border-radius:10px; min-width:260px;">
    <select id="origin" style="padding:8px 10px; border:1px solid var(--border); border-radius:10px;">
      <option value="">All origins</option>
    </select>
    <select id="ftype" style="padding:8px 10px; border:1px solid var(--border); border-radius:10px;">
      <option value="">All failure types</option>
    </select>
    <select id="fault" style="padding:8px 10px; border:1px solid var(--border); border-radius:10px;">
      <option value="">All fault levels</option>
    </select>
    <select id="success" style="padding:8px 10px; border:1px solid var(--border); border-radius:10px;">
      <option value="">All outcomes</option>
      <option value="true">Successful</option>
      <option value="false">Unsuccessful</option>
    </select>
    <button id="clear" style="padding:8px 12px; border:1px solid var(--border); border-radius:10px; background:#fff; cursor:pointer;">Clear</button>
    <span id="count" style="color:var(--muted); font-size:13px;"></span>
  </div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {
  const q = document.getElementById("q");
  const origin = document.getElementById("origin");
  const ftype = document.getElementById("ftype");
  const fault = document.getElementById("fault");
  const success = document.getElementById("success");
  const clear = document.getElementById("clear");
  const count = document.getElementById("count");

  function getRows() {
    return Array.from(document.querySelectorAll("tbody tr[data-problem-id]"));
  }

  function uniq(attr) {
    const rows = getRows();
    const s = new Set();
    rows.forEach(r => { const v = r.getAttribute(attr) || ""; if (v) s.add(v); });
    return Array.from(s).sort();
  }

  function fillSelect(sel, values) {
    while (sel.options.length > 1) sel.remove(1);
    values.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      sel.appendChild(opt);
    });
  }

  fillSelect(origin, uniq("data-origin"));
  fillSelect(ftype, uniq("data-failure-type"));
  fillSelect(fault, uniq("data-fault-level"));

  function apply() {
    const rows = getRows();
    const needle = (q.value || "").toLowerCase().trim();
    const o = origin.value;
    const t = ftype.value;
    const f = fault.value;
    const s = success.value;

    let shown = 0;
    rows.forEach(r => {
      const text = (r.getAttribute("data-search") || "").toLowerCase();
      const ok =
        (!needle || text.includes(needle)) &&
        (!o || r.getAttribute("data-origin") === o) &&
        (!t || r.getAttribute("data-failure-type") === t) &&
        (!f || r.getAttribute("data-fault-level") === f) &&
        (!s || r.getAttribute("data-successful") === s);

      r.style.display = ok ? "" : "none";
      if (ok) shown++;
    });

    count.textContent = shown + " / " + rows.length + " shown";
  }

  [q, origin, ftype, fault, success].forEach(el => el.addEventListener("input", apply));
  clear.addEventListener("click", () => {
    q.value = "";
    origin.value = "";
    ftype.value = "";
    fault.value = "";
    success.value = "";
    apply();
  });

  apply();
});
</script>
"""


INDEX_FILTER_UI = """
<div class='card'>
  <h3 style='margin:0 0 10px 0;'>Filter reports</h3>
  <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
    <input id="idx_q" placeholder="Search problem_id, file, namespace, application..." style="padding:8px 10px; border:1px solid var(--border); border-radius:10px; min-width:280px;">
    <select id="idx_namespace" style="padding:8px 10px; border:1px solid var(--border); border-radius:10px;">
      <option value="">All namespaces</option>
    </select>
    <select id="idx_application" style="padding:8px 10px; border:1px solid var(--border); border-radius:10px;">
      <option value="">All applications</option>
    </select>
    <select id="idx_overall" style="padding:8px 10px; border:1px solid var(--border); border-radius:10px;">
      <option value="">Overall outcome</option>
      <option value="true">Overall: true</option>
      <option value="false">Overall: false</option>
    </select>
    <button id="idx_clear" style="padding:8px 12px; border:1px solid var(--border); border-radius:10px; background:#fff; cursor:pointer;">Clear</button>
    <span id="idx_count" style="color:var(--muted); font-size:13px;"></span>
  </div>

  <div style="margin-top:12px;">
    <div style="color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; margin-bottom:6px;">
      Click a tag chip to filter
    </div>
    <div id="idx_tag_cloud" class="chips" style="flex-wrap:wrap;"></div>
  </div>
</div>
 <div style="margin:0 0 12px 0;">
    <a class="btn" href="analysis_report.html" target="_blank" rel="noopener">
      Open analysis report
    </a>
  </div>

<script>
document.addEventListener("DOMContentLoaded", function() {
  const q = document.getElementById("idx_q");
  const nsSel = document.getElementById("idx_namespace");
  const appSel = document.getElementById("idx_application");
  const overallSel = document.getElementById("idx_overall");
  const clear = document.getElementById("idx_clear");
  const count = document.getElementById("idx_count");
  const tagCloud = document.getElementById("idx_tag_cloud");

  // internal chip state (so chips work even if you don't have dropdowns for them)
  const chipState = {
    namespace: "",
    application: "",
    diagnosis: "",
    mitigation: "",
    overall: "",
  };

  function getRows() {
    return Array.from(document.querySelectorAll("tbody tr[data-problem-id]"));
  }

  function uniq(attr) {
    const rows = getRows();
    const s = new Set();
    rows.forEach(r => { const v = r.getAttribute(attr) || ""; if (v) s.add(v); });
    return Array.from(s).sort();
  }

  function fillSelect(sel, values) {
    while (sel.options.length > 1) sel.remove(1);
    values.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      sel.appendChild(opt);
    });
  }

  function syncFromSelects() {
    chipState.namespace = nsSel.value || "";
    chipState.application = appSel.value || "";
    chipState.overall = overallSel.value || "";
  }

  function setChip(key, value) {
    // toggle
    chipState[key] = (chipState[key] === value ? "" : value);

    // keep dropdowns in sync for these keys
    if (key === "namespace") nsSel.value = chipState.namespace;
    if (key === "application") appSel.value = chipState.application;
    if (key === "overall") overallSel.value = chipState.overall;

    apply();
  }

  function apply() {
    const rows = getRows();
    const needle = (q.value || "").toLowerCase().trim();

    // always let dropdowns override / match state
    syncFromSelects();

    let shown = 0;
    rows.forEach(r => {
      const text = (r.getAttribute("data-search") || "").toLowerCase();

      const ok =
        (!needle || text.includes(needle)) &&
        (!chipState.namespace || r.getAttribute("data-namespace") === chipState.namespace) &&
        (!chipState.application || r.getAttribute("data-application") === chipState.application) &&
        (!chipState.diagnosis || r.getAttribute("data-diagnosis") === chipState.diagnosis) &&
        (!chipState.mitigation || r.getAttribute("data-mitigation") === chipState.mitigation) &&
        (!chipState.overall || r.getAttribute("data-overall") === chipState.overall);

      r.style.display = ok ? "" : "none";
      if (ok) shown++;
    });

    count.textContent = shown + " / " + rows.length + " shown";

    document.querySelectorAll(".chip[data-key][data-value]").forEach(btn => {
      const k = btn.getAttribute("data-key");
      const v = btn.getAttribute("data-value");
      const active = k && v && chipState[k] === v;
      btn.classList.toggle("active", !!active);
    });
  }

  function addCloudSection(title, key, values) {
    if (!values.length) return;

    const header = document.createElement("div");
    header.textContent = title;
    header.style.cssText =
      "width:100%; margin:10px 0 6px 0; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em;";
    tagCloud.appendChild(header);

    values.slice(0, 160).forEach(v => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.setAttribute("data-key", key);
      b.setAttribute("data-value", v);
      b.textContent = title.toLowerCase() + ": " + v;
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        setChip(key, v);
      });
      tagCloud.appendChild(b);
    });
  }

  // Populate dropdowns + tag cloud (NOW that rows exist)
  fillSelect(nsSel, uniq("data-namespace"));
  fillSelect(appSel, uniq("data-application"));

  tagCloud.innerHTML = "";
  addCloudSection("Namespace", "namespace", uniq("data-namespace"));
  addCloudSection("Application", "application", uniq("data-application"));
  addCloudSection("Diagnosis", "diagnosis", ["true", "false"]);
  addCloudSection("Mitigation", "mitigation", ["true", "false"]);
  addCloudSection("Overall", "overall", ["true", "false"]);

  // Delegate: clicking chips inside the table row "Tags" column also filters
  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest(".chip[data-key][data-value]") : null;
    if (!btn) return;
    const k = btn.getAttribute("data-key");
    const v = btn.getAttribute("data-value");
    if (!k || !v) return;
    if (!Object.prototype.hasOwnProperty.call(chipState, k)) return;
    e.preventDefault();
    setChip(k, v);
  });

  [q, nsSel, appSel, overallSel].forEach(el => el.addEventListener("input", apply));
  clear.addEventListener("click", () => {
    q.value = "";
    nsSel.value = "";
    appSel.value = "";
    overallSel.value = "";
    chipState.namespace = "";
    chipState.application = "";
    chipState.diagnosis = "";
    chipState.mitigation = "";
    chipState.overall = "";
    apply();
  });

  apply();
});
</script>
"""


@dataclass
class SummaryRow:
    idx: int
    rec_type: str
    stage: str
    event_index: str
    submitted: str
    num_steps: str
    problem_id: str
    timestamp: str

    # from attributes.jsonl
    failure_type: str
    origin: str
    fault_level: str
    failure_level: str

    # parsed from messages
    namespace: str
    application: str

    # stage-specific + overall outcomes for filtering
    diagnosis_ok: str
    mitigation_ok: str
    resolution_ok: str
    overall_ok: str


@dataclass
class IndexRow:
    source_file: str
    link: str
    lines_scanned: int
    rendered: int
    parse_errors: int

    problem_id: str
    run: str          # e.g. "run_1", "run_2", or "" when no run_N in path
    agent: str        # e.g. "stratus", "claudecode", "codex", or ""
    origin: str
    failure_type: str
    fault_level: str
    failure_level: str
    namespace: str
    application: str

    diagnosis_ok: str
    mitigation_ok: str
    resolution_ok: str
    overall_ok: str


_NS_RE = re.compile(
    r"It belongs to this namespace:\s*(?:\n\s*|\s+)([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

_APP_RE = re.compile(
    r"You will be working this application:\s*(?:\n\s*|\s+)([^\n\r]+)",
    re.IGNORECASE,
)


def find_namespace(rec: dict[str, Any]) -> str:
    msgs = detect_messages(rec) or []
    for m in msgs:
        content = m.get("content", "")
        if isinstance(content, str):
            match = _NS_RE.search(content)
            if match:
                return match.group(1).strip()
    return ""


def find_application(rec: dict[str, Any]) -> str:
    msgs = detect_messages(rec) or []
    for m in msgs:
        content = m.get("content", "")
        if isinstance(content, str):
            match = _APP_RE.search(content)
            if match:
                app = match.group(1).strip()
                app = re.sub(r"\s+", " ", app)
                app = re.sub(r"\s+from messages\s*$", "", app, flags=re.IGNORECASE)
                return app
    return ""


def summarize_record(rec: dict[str, Any], idx: int, file_problem_id: str) -> SummaryRow:
    rec_type = as_str(rec.get("type"))
    stage = as_str(rec.get("stage"))
    event_index = as_str(rec.get("event_index"))
    submitted = as_str(rec.get("submitted"))
    num_steps = as_str(rec.get("num_steps"))
    problem_id = as_str(rec.get("problem_id")) or file_problem_id
    timestamp = as_str(rec.get("timestamp_readable") or rec.get("timestamp"))

    namespace = find_namespace(rec) or "default"
    application = find_application(rec) or "unknown"

    data = ATTR_INDEX.get(problem_id, {}) if problem_id else {}
    failure_type = as_str(data.get("type"))
    origin = as_str(data.get("origin"))
    fault_level = as_str(data.get("fault_level"))
    failure_level = as_str(data.get("failure_level"))

    diag_ok = False
    mit_ok = False
    res_ok = False
    ov_ok = False
    if problem_id:
        try:
            diag_ok = diagnosis_success(problem_id)
            mit_ok = mitigation_success(problem_id)
            res_ok = resolution_success(problem_id)
            ov_ok = diag_ok and mit_ok
        except Exception:
            diag_ok = False
            mit_ok = False
            res_ok = False
            ov_ok = False

    if problem_id and problem_id not in tags_by_problem_id:
        tags_by_problem_id[problem_id] = Tags(
            namespace=namespace,
            application=application,
            diagnosis_success=diag_ok,
            mitigation_success=mit_ok,
            resolution_success=res_ok,
            overall_success=ov_ok,
        )

    return SummaryRow(
        idx=idx,
        rec_type=rec_type,
        stage=stage,
        event_index=event_index,
        submitted=submitted,
        num_steps=num_steps,
        problem_id=problem_id,
        timestamp=timestamp,
        failure_type=failure_type,
        origin=origin,
        fault_level=fault_level,
        failure_level=failure_level,
        namespace=namespace,
        application=application,
        diagnosis_ok="true" if diag_ok else "false",
        mitigation_ok="true" if mit_ok else "false",
        resolution_ok="true" if res_ok else "false",
        overall_ok="true" if ov_ok else "false",
    )


def summarize_index_row(
    source_file: str,
    link: str,
    lines_scanned: int,
    rendered: int,
    parse_errors: int,
    problem_id: str,
    run: str = "",
    agent: str = "",
) -> IndexRow:
    data = ATTR_INDEX.get(problem_id, {}) if problem_id else {}

    failure_type = as_str(data.get("type"))
    origin = as_str(data.get("origin"))
    fault_level = as_str(data.get("fault_level"))
    failure_level = as_str(data.get("failure_level"))

    t = tags_by_problem_id.get(problem_id)
    if t is not None:
        namespace = as_str(t.namespace) or "default"
        application = as_str(t.application) or "unknown"
        diag_ok = bool(t.diagnosis_success)
        mit_ok = bool(t.mitigation_success)
        res_ok = bool(t.resolution_success)
        ov_ok = bool(t.overall_success)
    else:
        namespace = "default"
        application = "unknown"
        diag_ok = False
        mit_ok = False
        res_ok = False
        ov_ok = False

    return IndexRow(
        source_file=source_file,
        link=link,
        lines_scanned=lines_scanned,
        rendered=rendered,
        parse_errors=parse_errors,
        problem_id=problem_id,
        run=run,
        agent=agent,
        origin=origin,
        failure_type=failure_type,
        fault_level=fault_level,
        failure_level=failure_level,
        namespace=namespace,
        application=application,
        diagnosis_ok="true" if diag_ok else "false",
        mitigation_ok="true" if mit_ok else "false",
        resolution_ok="true" if res_ok else "false",
        overall_ok="true" if ov_ok else "false",
    )


HIGHLIGHT = """
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>window.addEventListener('load', () => hljs.highlightAll());</script>
"""

BASE_CSS = """
<style>
:root { --bg:#ffffff; --fg:#111; --muted:#666; --card:#f7f7f9; --border:#e6e6ea; }
* { box-sizing: border-box; }
html, body { max-width: 100%; overflow-x: hidden; }
body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; color: var(--fg); background: var(--bg); }
header { padding: 18px 22px; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: rgba(255,255,255,0.92); backdrop-filter: blur(6px); }
h1 { margin: 0; font-size: 18px; }
small { color: var(--muted); }

/* Make page fit window (no fixed 1200px) */
main { padding: 18px 22px; width: 100%; max-width: 100%; margin: 0; }

/* Cards */
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin: 12px 0; }

/* Tables: fixed layout + wrap everywhere so no horizontal scroll */
.table { width: 100%; border-collapse: collapse; table-layout: fixed; }
.table th, .table td {
  text-align: left;
  padding: 10px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  font-size: 13px;

  overflow-wrap: anywhere;
  word-break: break-word;
}
.table td { max-width: 0; }
.table th { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }

/* Links + monospace wrapping */
a { color: #0b5fff; text-decoration: none; overflow-wrap:anywhere; word-break: break-word; }
a:hover { text-decoration: underline; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; overflow-wrap:anywhere; word-break: break-word; }
.btn{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:10px 14px;
  border-radius:12px;
  border:1px solid var(--border);
  background:#fff;
  color:#0b5fff;
  font-weight:650;
  font-size:13px;
  text-decoration:none;
  cursor:pointer;
}
.btn:hover{
  border-color: #0b5fff55;
  background:#0b5fff0a;
  text-decoration:none;
}

/* Code blocks also wrap */
details > summary { cursor: pointer; color: var(--muted); }
pre { overflow-x: auto; white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; }
pre code { white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; }
.msg .content { white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; }

/* Layout */
.grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
@media (min-width: 900px) {
  .grid { grid-template-columns: minmax(260px, 360px) 1fr; align-items: start; }
}

/* Messages */
.msg { border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px; background: #fff; margin-bottom: 10px; }
.msg .role { font-size: 12px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: .04em; }
.msg.user { border-left: 5px solid #0b5fff22; }
.msg.assistant { border-left: 5px solid #16a34a22; }
.msg.tool { border-left: 5px solid #f59e0b22; }
.kv { display: grid; grid-template-columns: 170px 1fr; gap: 6px 12px; font-size: 13px; }
hr { border: 0; border-top: 1px solid var(--border); margin: 18px 0; }
.msg.tool, .msg.tool .content, .msg.tool pre, .msg.tool code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
.msg.tool { background: #fffdf5; }
.kv .k { color: var(--muted); }
.kv .k.hot { color: var(--fg); font-weight: 650; background: #ffffff; border: 1px solid var(--border); border-radius: 8px; padding: 2px 8px; display: inline-block; }
.badge { display: inline-block; padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px; font-size: 12px; margin-right: 6px; background: #fff; max-width: 100%; overflow-wrap:anywhere; }
.badge.hot { border-color: #0b5fff55; background: #0b5fff0a; font-weight: 650; }

/* --- Tag chips --- */
.chips { display:flex; gap:8px; flex-wrap:wrap; align-items:center; min-width: 0; }
.chip {
  display:inline-flex;
  align-items:center;
  gap:6px;
  padding:6px 10px;
  border:1px solid var(--border);
  border-radius:999px;
  background:#fff;
  cursor:pointer;
  font-size:12px;
  line-height:1.1;
  max-width: 100%;
  overflow-wrap:anywhere;
  word-break: break-word;
}
.chip:hover { border-color:#0b5fff55; background:#0b5fff0a; }
.chip.active { border-color:#0b5fff99; background:#0b5fff14; font-weight:650; }
</style>
"""


def html_page(title: str, body: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
{BASE_CSS}
{HIGHLIGHT}
</head>
<body>
<header>
  <h1>{escape(title)}</h1>
  <small>Generated {escape(now)} • Rendering highest event_index per stage (diagnosis + all mitigation attempts)</small>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def render_messages(msgs: list[dict[str, Any]]) -> str:
    out = ["<div class='card'><h3 style='margin:0 0 10px 0;'>Messages</h3>"]

    for m in msgs:
        mtype = as_str(m.get("role") or m.get("type") or "message").strip()
        mtype_l = mtype.lower()

        cls = ""
        if "system" in mtype_l:
            cls = "tool"
        elif "human" in mtype_l or "user" in mtype_l:
            cls = "user"
        elif "tool" in mtype_l:
            cls = "tool"
        elif "ai" in mtype_l or "assistant" in mtype_l:
            cls = "assistant"

        content = m.get("content")
        if isinstance(content, list):
            content_str = json.dumps(content, ensure_ascii=False, indent=2)
        else:
            content_str = "" if content is None else str(content)

        tool_calls = m.get("tool_calls")
        if tool_calls is None and isinstance(m.get("additional_kwargs"), dict):
            tool_calls = m["additional_kwargs"].get("tool_calls")

        body_parts: list[str] = []

        if tool_calls:
            try:
                tool_calls_json = pretty_json(tool_calls)
            except Exception:
                tool_calls_json = json.dumps(tool_calls, ensure_ascii=False, indent=2)
            body_parts.append(
                "<div style='margin-top:6px;'>"
                "<div class='mono' style='color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em;'>tool_calls</div>"
                "<pre><code class='language-json'>" + escape(tool_calls_json) + "</code></pre>"
                "</div>"
            )

        if content_str.strip():
            content_div_cls = "content mono" if cls == "tool" else "content"
            body_parts.append(
                f"<div class='{content_div_cls}' style='white-space:pre-wrap'>{escape(content_str)}</div>"
            )

        if not body_parts:
            body_parts.append("<div class='content'><small>(empty)</small></div>")

        out.append(f"<div class='msg {cls}'><div class='role'>{escape(mtype)}</div>" + "\n".join(body_parts) + "</div>")

    out.append("</div>")
    return "\n".join(out)


def render_kv(rec: dict[str, Any], exclude_keys: set) -> str:
    items: list[tuple[str, str]] = []
    for k, v in rec.items():
        if k in exclude_keys:
            continue
        v_str = as_str(v, 300) if isinstance(v, (dict, list)) else as_str(v, 500)
        items.append((str(k), v_str))

    if not items:
        return ""

    html = ["<div class='card'><h3 style='margin:0 0 10px 0;'>Top-level fields</h3><div class='kv'>"]
    for k, v in items[:60]:
        key_cls = "k hot" if k in HOT_KEYS else "k"
        html.append(f"<div><span class='{key_cls}'>{escape(k)}</span></div><div>{escape(v)}</div>")

    if len(items) > 60:
        html.append(f"<div></div><div><small>+ {len(items) - 60} more fields not shown</small></div>")
    html.append("</div></div>")
    return "\n".join(html)


def chip(label: str, value: str, key: str) -> str:
    if not value:
        return ""
    return (
        f"<button class='chip' type='button' "
        f"data-key='{escape(key)}' data-value='{escape(value)}'>"
        f"{escape(label)}: <span class='mono'>{escape(value)}</span>"
        f"</button>"
    )


def render_index_chips(r: IndexRow) -> str:
    parts = []
    parts.append(chip("origin", r.origin, "origin"))
    parts.append(chip("type", r.failure_type, "failure_type"))
    parts.append(chip("fault", r.fault_level, "fault_level"))
    parts.append(chip("level", r.failure_level, "failure_level"))
    parts.append(chip("ns", r.namespace, "namespace"))
    parts.append(chip("app", r.application, "application"))
    parts.append(chip("diag", r.diagnosis_ok, "diagnosis"))
    parts.append(chip("mit", r.mitigation_ok, "mitigation"))
    parts.append(chip("res", r.resolution_ok, "resolution"))
    parts.append(chip("overall", r.overall_ok, "overall"))
    parts = [p for p in parts if p]
    if not parts:
        return "<small>(no tags)</small>"
    return "<div class='chips'>" + "\n".join(parts) + "</div>"


def render_file_report(
    file_name: str,
    records: list[dict[str, Any]],
    parse_errors: list[str],
    total_lines_scanned: int,
    file_problem_id: str,
) -> str:
    rows = [summarize_record(r, i + 1, file_problem_id=file_problem_id) for i, r in enumerate(records)]

    event_mode = False
    if records:
        event_hits = sum(1 for r in records if is_event_record(r))
        event_mode = event_hits >= max(1, int(0.6 * len(records)))

    if event_mode:
        table = [
            "<div class='card'>",
            "<h3 style='margin:0 0 10px 0;'>Investigation Timeline</h3>",
            f"<small>Source: <span class='mono'>{escape(file_name)}</span> • Scanned {total_lines_scanned} lines • Rendered {len(records)} event(s)</small>",
            "<div style='height:10px'></div>",
            FILTER_UI,
            "<table class='table'>",
            "<thead><tr>"
            "<th>#</th><th>Stage</th><th>Event #</th><th>Submitted</th><th>Steps</th><th>Last message</th><th>Problem</th><th>Timestamp</th>"
            "</tr></thead><tbody>",
        ]
        for r in rows:
            anchor = f"evt-{r.idx}"
            lm = last_message_preview(records[r.idx - 1])

            search_blob = " | ".join(
                [
                    r.problem_id,
                    r.namespace,
                    r.application,
                    r.diagnosis_ok,
                    r.mitigation_ok,
                    r.resolution_ok,
                    r.overall_ok,
                ]
            )

            table.append(
                f"<tr data-problem-id='{escape(r.problem_id)}' "
                f"data-origin='{escape(r.origin)}' "
                f"data-failure-type='{escape(r.failure_type)}' "
                f"data-fault-level='{escape(r.fault_level)}' "
                f"data-failure-level='{escape(r.failure_level)}' "
                f"data-namespace='{escape(r.namespace)}' "
                f"data-application='{escape(r.application)}' "
                f"data-successful='{escape(r.overall_ok)}' "
                f"data-search='{escape(search_blob)}'>"
                f"<td>{r.idx}</td>"
                f"<td><a href='#{anchor}'>{escape(r.stage or '(no stage)')}</a></td>"
                f"<td>{escape(r.event_index)}</td>"
                f"<td>{escape(r.submitted)}</td>"
                f"<td>{escape(r.num_steps)}</td>"
                f"<td>{escape(lm)}</td>"
                f"<td>{escape(r.problem_id)}</td>"
                f"<td>{escape(r.timestamp)}</td>"
                "</tr>"
            )
        table.append("</tbody></table></div>")
    else:
        table = [
            "<div class='card'>",
            "<h3 style='margin:0 0 10px 0;'>Investigation Entries</h3>",
            f"<small>Source: <span class='mono'>{escape(file_name)}</span> • Scanned {total_lines_scanned} lines • Rendered {len(records)} entry(ies)</small>",
            "<div style='height:10px'></div>",
            FILTER_UI,
            "<table class='table'>",
            "<thead><tr>"
            "<th>#</th><th>Type</th><th>Stage</th><th>Event #</th><th>Submitted</th><th>Steps</th><th>Problem</th><th>Timestamp</th>"
            "</tr></thead><tbody>",
        ]
        for r in rows:
            anchor = f"evt-{r.idx}"

            search_blob = " | ".join(
                [
                    r.problem_id,
                    r.failure_type,
                    r.origin,
                    r.fault_level,
                    r.failure_level,
                    r.namespace,
                    r.application,
                    r.stage,
                    r.rec_type,
                    r.diagnosis_ok,
                    r.mitigation_ok,
                    r.resolution_ok,
                    r.overall_ok,
                ]
            )

            table.append(
                f"<tr data-problem-id='{escape(r.problem_id)}' "
                f"data-origin='{escape(r.origin)}' "
                f"data-failure-type='{escape(r.failure_type)}' "
                f"data-fault-level='{escape(r.fault_level)}' "
                f"data-failure-level='{escape(r.failure_level)}' "
                f"data-namespace='{escape(r.namespace)}' "
                f"data-application='{escape(r.application)}' "
                f"data-successful='{escape(r.overall_ok)}' "
                f"data-search='{escape(search_blob)}'>"
                f"<td>{r.idx}</td>"
                f"<td><a href='#{anchor}'>{escape(r.rec_type or ('entry-' + str(r.idx)))}</a></td>"
                f"<td>{escape(r.stage)}</td>"
                f"<td>{escape(r.event_index)}</td>"
                f"<td>{escape(r.submitted)}</td>"
                f"<td>{escape(r.num_steps)}</td>"
                f"<td>{escape(r.problem_id)}</td>"
                f"<td>{escape(r.timestamp)}</td>"
                "</tr>"
            )
        table.append("</tbody></table></div>")

    parts = ["".join(table)]

    if parse_errors:
        parts.append(
            "<div class='card'><h3 style='margin:0 0 10px 0;'>Parse errors</h3><pre>"
            + escape("\n".join(parse_errors))
            + "</pre></div>"
        )

    for i, rec in enumerate(records, start=1):
        s = summarize_record(rec, i, file_problem_id=file_problem_id)
        msgs = detect_messages(rec)
        steps = detect_steps(rec)

        exclude = set()
        if msgs is not None:
            exclude.add("messages")
            if "last_message" in rec:
                exclude.add("last_message")

        if steps is not None:
            for k in ["steps", "events", "trace", "spans"]:
                if k in rec:
                    exclude.add(k)

        header_left = "Investigation Event"
        subtitle = ""
        if event_mode and (s.stage or s.event_index):
            header_left = f"Investigation • Stage {s.stage or '?'} • Event {s.event_index or i}"
            subtitle = as_str(rec.get("type") or "")

        badges = (
            (f'<span class="badge hot">type: {escape(s.rec_type)}</span>' if s.rec_type else "")
            + (f'<span class="badge hot">stage: {escape(s.stage)}</span>' if s.stage else "")
            + (f'<span class="badge hot">event: {escape(s.event_index)}</span>' if s.event_index else "")
            + (f'<span class="badge hot">submitted: {escape(s.submitted)}</span>' if s.submitted else "")
            + (f'<span class="badge hot">steps: {escape(s.num_steps)}</span>' if s.num_steps else "")
            + (f'<span class="badge hot">problem: {escape(s.problem_id)}</span>' if s.problem_id else "")
            + (f'<span class="badge hot">time: {escape(s.timestamp)}</span>' if s.timestamp else "")
        )

        parts.append(
            f"<hr><div id='evt-{i}' class='card'>"
            f"<div style='display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap;'>"
            f"<div><h2 style='margin:0;'>{escape(header_left)}</h2>"
            f"<small>{escape(subtitle)}</small></div>"
            f"<div>{badges}</div>"
            f"</div></div>"
        )

        parts.append(
            "<div class='card'>"
            "<h3 style='margin:0 0 10px 0;'>Problem metadata</h3>"
            "<div class='kv'>"
            f"<div class='k'>Origin</div><div>{escape(s.origin)}</div>"
            f"<div class='k'>Failure Type</div><div>{escape(s.failure_type)}</div>"
            f"<div class='k'>Fault Level</div><div>{escape(s.fault_level)}</div>"
            f"<div class='k'>Failure Level</div><div>{escape(s.failure_level)}</div>"
            f"<div class='k'>Namespace</div><div>{escape(s.namespace)}</div>"
            f"<div class='k'>Application</div><div>{escape(s.application)}</div>"
            f"<div class='k'>Diagnosis</div><div>{escape(s.diagnosis_ok)}</div>"
            f"<div class='k'>Mitigation</div><div>{escape(s.mitigation_ok)}</div>"
            f"<div class='k'>Resolution</div><div>{escape(s.resolution_ok)}</div>"
            f"<div class='k'>Overall</div><div>{escape(s.overall_ok)}</div>"
            "</div></div>"
        )

        parts.append("<div class='grid'>")
        parts.append(render_kv(rec, exclude_keys=exclude))

        if msgs is not None:
            parts.append(render_messages(msgs))
        elif steps is not None:
            parts.append(
                "<div class='card'><h3 style='margin:0 0 10px 0;'>Steps / Events (preview)</h3>"
                "<pre><code class='language-json'>" + escape(pretty_json(steps[:50])) + "</code></pre>"
                "<small>Showing up to first 50 items.</small></div>"
            )

        parts.append("</div></div>")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Raw agent output conversion (claude-code.txt / codex.txt → trajectory JSONL)
# ---------------------------------------------------------------------------


def _cc_extract_text(content: list[dict]) -> str:
    """Join all text blocks from a content list (Claude Code format)."""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _cc_parse_stream_json(input_path: Path) -> tuple[list[dict[str, Any]], bool]:
    """
    Parse a Claude Code stream-json output file into a flat list of messages.
    Returns (messages, submitted).
    """
    messages: list[dict[str, Any]] = []
    submitted = False

    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "system" and event.get("subtype") == "init":
                model = event.get("model", "")
                cwd = event.get("cwd", "")
                if model or cwd:
                    messages.append({"role": "system", "content": f"model={model} cwd={cwd}"})

            elif etype in ("user", "assistant"):
                msg = event.get("message") or {}
                role = msg.get("role", etype)
                content = msg.get("content", "")

                if isinstance(content, str):
                    if content.strip():
                        messages.append({"role": role, "content": content})
                    continue

                if not isinstance(content, list):
                    continue

                text_parts: list[str] = []
                tool_calls: list[dict] = []
                tool_results: list[dict] = []

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            text_parts.append(text)
                    elif btype == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "args": block.get("input", {}),
                                "type": "tool_call",
                            }
                        )
                    elif btype == "tool_result":
                        tool_results.append(block)

                if tool_results:
                    for tr in tool_results:
                        tr_content = tr.get("content", "")
                        if isinstance(tr_content, list):
                            tr_content = "\n".join(
                                b.get("text", "") for b in tr_content if isinstance(b, dict)
                            )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_use_id": tr.get("tool_use_id", ""),
                                "content": tr_content,
                            }
                        )
                else:
                    m: dict[str, Any] = {
                        "role": role,
                        "content": "\n".join(text_parts),
                    }
                    if tool_calls:
                        m["tool_calls"] = tool_calls
                    messages.append(m)

            elif etype == "result":
                submitted = event.get("subtype") == "success"

    return messages, submitted


def _codex_parse_json(input_path: Path) -> tuple[list[dict[str, Any]], bool]:
    """
    Parse a Codex output file into a flat list of messages.

    The file format is newline-delimited JSON (JSONL).  The very first line is
    the plain-text banner "Reading additional input from stdin..." and is not
    valid JSON — it is silently skipped.

    Relevant event types:
      item.completed / item.type=agent_message   → assistant narration text
      item.started   / item.type=command_execution → tool call (shell command)
      item.completed / item.type=command_execution → tool result (aggregated_output)

    Multiple commands can be in-flight simultaneously and complete out of order.
    The parser uses a two-phase approach:
      1. Collect all events into a list (skipping non-JSON lines).
      2. Walk the list maintaining a pending assistant turn; flush it (with its
         accumulated tool_calls) when the first tool result for that turn arrives.

    Returns (messages, submitted).
    """
    raw_events: list[dict] = []
    with input_path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                raw_events.append(event)

    messages: list[dict[str, Any]] = []
    submitted = False

    # Pending assistant turn (not yet emitted).
    pending_text: str | None = None
    pending_calls: list[dict] = []
    assistant_flushed = True  # True once pending_text has been written to messages

    def flush_assistant() -> None:
        nonlocal assistant_flushed
        if assistant_flushed:
            return
        msg: dict[str, Any] = {"role": "assistant", "content": pending_text or ""}
        if pending_calls:
            msg["tool_calls"] = list(pending_calls)
        messages.append(msg)
        assistant_flushed = True

    for event in raw_events:
        etype = event.get("type", "")
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")

        if etype == "item.completed" and itype == "agent_message":
            # A new narration block starts a new assistant turn.
            # If the previous turn never got tool calls, emit it as text-only now.
            if not assistant_flushed:
                flush_assistant()
            pending_text = item.get("text", "")
            pending_calls = []
            assistant_flushed = False

        elif etype == "item.started" and itype == "command_execution":
            command = item.get("command", "")
            pending_calls.append(
                {
                    "id": item.get("id", ""),
                    "name": "bash",
                    "args": {"command": command},
                    "type": "tool_call",
                }
            )

        elif etype == "item.completed" and itype == "command_execution":
            # Flush the pending assistant message (with tool_calls) before the
            # first result of that turn arrives.
            flush_assistant()

            output = item.get("aggregated_output", "") or ""
            exit_code = item.get("exit_code")
            if exit_code is not None and exit_code != 0:
                content = f"[exit {exit_code}]\n{output}" if output else f"[exit {exit_code}]"
            else:
                content = output

            messages.append(
                {
                    "role": "tool",
                    "tool_use_id": item.get("id", ""),
                    "content": content,
                }
            )

            # Detect a successful submit call.
            command = item.get("command", "")
            if "/submit" in command and exit_code == 0:
                submitted = True

    # Flush any trailing assistant turn that never triggered a tool result.
    flush_assistant()

    return messages, submitted


def _build_trajectory_events(
    messages: list[dict[str, Any]],
    submitted: bool,
    stage: str,
    problem_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """
    Build incremental event records where each event contains the cumulative
    message history up to that tool-result boundary.
    """
    events: list[dict[str, Any]] = []
    event_index = 0
    num_steps = 0
    accumulated: list[dict] = []

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        accumulated.append(msg)

        if role == "tool":
            num_steps += 1
            while i + 1 < len(messages) and messages[i + 1].get("role") == "tool":
                i += 1
                accumulated.append(messages[i])
                num_steps += 1

            is_last = i == len(messages) - 1
            events.append(
                {
                    "type": "event",
                    "stage": stage,
                    "event_index": event_index,
                    "num_steps": num_steps,
                    "submitted": submitted if is_last else False,
                    "rollback_stack": "",
                    "messages": list(accumulated),
                    "last_message": accumulated[-1],
                    "problem_id": problem_id,
                    "timestamp": timestamp,
                }
            )
            event_index += 1

        i += 1

    if not events or accumulated != events[-1]["messages"]:
        events.append(
            {
                "type": "event",
                "stage": stage,
                "event_index": event_index,
                "num_steps": num_steps,
                "submitted": submitted,
                "rollback_stack": "",
                "messages": list(accumulated),
                "last_message": accumulated[-1] if accumulated else {},
                "problem_id": problem_id,
                "timestamp": timestamp,
            }
        )

    return events


def _convert_agent_file(
    input_path: Path,
    output_path: Path,
    problem_id: str,
    agent: str,
    stage: str = "diagnosis",
) -> Path:
    """
    Convert a raw agent output file (claude-code.txt or codex.txt) to
    a stratus-format trajectory JSONL.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    timestamp = now.strftime("%m%d_%H%M")
    timestamp_readable = now.strftime("%Y-%m-%d %H:%M:%S")

    if agent == "claudecode":
        messages, submitted = _cc_parse_stream_json(input_path)
    elif agent == "codex":
        messages, submitted = _codex_parse_json(input_path)
    else:
        raise ValueError(f"Unknown agent type for conversion: {agent}")

    events = _build_trajectory_events(messages, submitted, stage, problem_id, timestamp)

    with output_path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "metadata",
                    "problem_id": problem_id,
                    "timestamp": timestamp,
                    "timestamp_readable": timestamp_readable,
                    "total_stages": 1,
                    "total_events": len(events),
                    "agent": agent,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {"type": "stage_start", "stage": stage, "num_events": len(events)},
                ensure_ascii=False,
            )
            + "\n"
        )
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"[{agent}_to_trajectory] Wrote {len(events)} event(s) → {output_path}")
    return output_path


def _extract_problem_id_from_path(run_dir: Path) -> str:
    """
    Derive problem_id from directory structure:
      results/{ts}/{agent}/{problem_id}/run_{N}/
    """
    for part in reversed(run_dir.parts):
        if _RUN_LABEL_RE.match(part):
            idx = list(run_dir.parts).index(part)
            if idx > 0:
                return run_dir.parts[idx - 1]
    return run_dir.parent.name


def _trajectory_has_messages(traj_path: Path) -> bool:
    """Return True if the trajectory file contains at least one event with non-empty messages."""
    try:
        with traj_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "event" and obj.get("messages"):
                    return True
    except OSError:
        pass
    return False


def _convert_raw_agent_outputs(root: Path) -> None:
    """
    Walk a results directory and convert any claude-code.txt / codex.txt files
    that don't yet have a valid *_trajectory.jsonl (one with actual messages).
    """
    _TS_RE = re.compile(r"^\d{4}_\d{4}$")

    def _needs_conversion(run_dir: Path) -> bool:
        trajs = list(run_dir.rglob("*_trajectory.jsonl"))
        return not trajs or not any(_trajectory_has_messages(t) for t in trajs)

    tasks: list[tuple[str, Path, Path]] = []
    for output_file in sorted(root.rglob("claude-code.txt")):
        run_dir = output_file.parent
        if _needs_conversion(run_dir):
            tasks.append(("claudecode", output_file, run_dir))

    for output_file in sorted(root.rglob("codex.txt")):
        run_dir = output_file.parent
        if _needs_conversion(run_dir):
            tasks.append(("codex", output_file, run_dir))

    if not tasks:
        return

    for agent, output_file, run_dir in tasks:
        problem_id = _extract_problem_id_from_path(run_dir)
        traj_dir = run_dir / "trajectory"
        ts = ""
        for part in run_dir.parts:
            if _TS_RE.match(part):
                ts = part
                break
        out_name = (
            f"{ts}_{problem_id}_{agent}_agent_trajectory.jsonl"
            if ts
            else f"{problem_id}_{agent}_agent_trajectory.jsonl"
        )
        out_path = traj_dir / out_name

        print(f"Converting {output_file} → {out_path}")
        try:
            _convert_agent_file(
                input_path=output_file,
                output_path=out_path,
                problem_id=problem_id,
                agent=agent,
            )
        except Exception as exc:
            print(f"  ERROR: {exc}")


def main():
    ap = argparse.ArgumentParser(
        description="Convert JSONL files to readable HTML reports (only highest event_index for target stages)."
    )
    ap.add_argument("inputs", nargs="+", help="Input .jsonl file(s) or directories containing .jsonl")
    ap.add_argument("-o", "--out", default="html_reports", help="Output directory")
    args = ap.parse_args()

    # Load results.csv from the provided root (NOT the script directory)
    # global all_results_csv
    # results_csv_path = pick_results_csv_with_most_rows(root)
    # all_results_csv = pd.read_csv(results_csv_path)
    # pd.set_option("display.max_columns", None)

    # Load attributes.jsonl from the provided root (fallback to script dir)
    # global ATTR_INDEX
    # ATTR_INDEX = load_attributes_index(root)
    #
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Run analysis report generation in the provided root so relative paths work
    # generate_analysis_report(root)

    # Copy analysis_report.html from root into the output dir (if present)
    # src = root / "analysis_report.html"
    # destination = out_dir / "analysis_report.html"
    # if src.exists():
    #     shutil.copy2(src, destination)

    # Auto-convert any agent output files (claude-code.txt, codex.txt)
    # that don't yet have a trajectory JSONL.
    for inp in args.inputs:
        p = Path(inp).expanduser().resolve()
        if p.is_dir():
            _convert_raw_agent_outputs(p)

    jsonl_files: list[Path] = []
    for inp in args.inputs:
        p = Path(inp).expanduser().resolve()
        if p.is_dir():
            # Only collect trajectory files; session/internal JSONL files
            # (e.g. Claude Code's sessions/projects/-app/*.jsonl) are excluded.
            jsonl_files.extend(sorted(p.rglob("*_trajectory.jsonl")))
        elif p.is_file() and p.suffix.lower() == ".jsonl":
            jsonl_files.append(p)
        else:
            print(f"Skipping (not .jsonl or dir): {p}")

    if not jsonl_files:
        raise SystemExit(
            "No trajectory JSONL files found.\n"
            "Expected files matching *_trajectory.jsonl under the given path.\n"
            "Make sure a run has completed successfully so trajectory files are generated."
        )

    index_rows: list[IndexRow] = []
    all_parse_errors: list[str] = []

    for fpath in jsonl_files:
        # Discover all stages present in this file (diagnosis + all mitigation_attempt_N)
        stages = discover_stages(fpath) or TARGET_STAGES_ORDER
        records, errors, total_lines = stream_pick_highest_event_index_per_stage(fpath, stages)
        file_pid = find_problem_id(fpath)
        all_parse_errors.extend(errors)

        run_label = extract_run_label(fpath)
        agent_label = extract_agent_label(fpath)
        # Prefix output filename with agent+run to avoid collisions across agents/runs
        stem = safe_filename(fpath.stem)
        prefix_parts = [p for p in [safe_filename(agent_label), safe_filename(run_label)] if p]
        prefix = ("_".join(prefix_parts) + "_") if prefix_parts else ""
        out_file = out_dir / f"{prefix}{stem}.html"

        body = render_file_report(fpath.name, records, errors, total_lines, file_problem_id=file_pid)
        html = html_page(f"{fpath.name} — Investigation Report", body)
        out_file.write_text(html, encoding="utf-8")

        # Save tool calls to CSV
        save_tool_calls_csv(records, out_dir, prefix)

        pid = ""
        if records:
            pid = as_str(records[0].get("problem_id"))
        if not pid:
            pid = find_problem_id(fpath)

        index_rows.append(
            summarize_index_row(
                source_file=fpath.name,
                link=out_file.name,
                lines_scanned=total_lines,
                rendered=len(records),
                parse_errors=len(errors),
                problem_id=pid,
                run=run_label,
                agent=agent_label,
            )
        )

    # Sort index: group by problem_id, then agent, then run number, then filename
    def _run_sort_key(r: IndexRow) -> tuple:
        m = re.search(r"(\d+)$", r.run)
        run_num = int(m.group(1)) if m else 0
        return (r.problem_id, r.agent, run_num, r.source_file)

    index_rows.sort(key=_run_sort_key)

    idx = [
        "<div class='card'><h3 style='margin:0 0 10px 0;'>Reports</h3>",
        "<small>Click chips to filter. Rows grouped by problem → run.</small>",
        "</div>",
        INDEX_FILTER_UI,
        "<div class='card'>",
        "<table class='table'><thead><tr>"
        "<th>Problem</th><th>Agent</th><th>Run</th><th>Source file</th><th>Tags</th>"
        "<th>Stages rendered</th><th>Lines scanned</th><th>Parse errors</th>"
        "</tr></thead><tbody>",
    ]

    prev_group = None
    for r in index_rows:
        # Visual separator between problem groups
        group = r.problem_id
        if group != prev_group:
            if prev_group is not None:
                idx.append(
                    "<tr><td colspan='8' style='padding:4px 0;border:none;"
                    "background:var(--border)'></td></tr>"
                )
            prev_group = group

        search_blob = " | ".join(
            [
                r.source_file,
                r.link,
                r.problem_id,
                r.agent,
                r.run,
                r.origin,
                r.failure_type,
                r.fault_level,
                r.failure_level,
                r.namespace,
                r.application,
                r.diagnosis_ok,
                r.mitigation_ok,
                r.resolution_ok,
                r.overall_ok,
            ]
        )

        idx.append(
            "<tr "
            f"data-problem-id='{escape(r.problem_id)}' "
            f"data-agent='{escape(r.agent)}' "
            f"data-origin='{escape(r.origin)}' "
            f"data-failure-type='{escape(r.failure_type)}' "
            f"data-fault-level='{escape(r.fault_level)}' "
            f"data-failure-level='{escape(r.failure_level)}' "
            f"data-namespace='{escape(r.namespace)}' "
            f"data-application='{escape(r.application)}' "
            f"data-diagnosis='{escape(r.diagnosis_ok)}' "
            f"data-mitigation='{escape(r.mitigation_ok)}' "
            f"data-resolution='{escape(r.resolution_ok)}' "
            f"data-overall='{escape(r.overall_ok)}' "
            f"data-search='{escape(search_blob)}'>"
            f"<td class='mono'>{escape(r.problem_id)}</td>"
            f"<td class='mono'>{escape(r.agent) if r.agent else '—'}</td>"
            f"<td class='mono'>{escape(r.run) if r.run else '—'}</td>"
            f"<td><a href='{escape(r.link)}'>{escape(r.source_file)}</a></td>"
            f"<td>{render_index_chips(r)}</td>"
            f"<td>{r.rendered}</td>"
            f"<td>{r.lines_scanned}</td>"
            f"<td>{r.parse_errors}</td>"
            "</tr>"
        )

    idx.append("</tbody></table></div></div>")

    if all_parse_errors:
        idx.append(
            "<div class='card'><details><summary>All parse errors</summary><pre>"
            + escape("\n".join(all_parse_errors))
            + "</pre></details></div>"
        )

    (out_dir / "index.html").write_text(
        html_page("Investigation Reports (Highest event_index only)", "\n".join(idx)),
        encoding="utf-8",
    )

    print(f"Done. Open: {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
