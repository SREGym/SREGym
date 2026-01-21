import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import subprocess
import pandas as pd
import shutil
# Keep ONLY the single highest-event_index "event" record per stage (per file),
# but render the FULL event using your existing HTML logic.
@dataclass(frozen=True)
class Tags:
    namespace: str
    application: str
    diagnosis_success: bool
    mitigation_success: bool
    overall_success: bool

TARGET_STAGES_ORDER = ["diagnosis", "mitigation_attempt_0"]
all_results_csv: Optional[pd.DataFrame] = None
ATTR_INDEX: Dict[str, Dict[str, Any]] = {}
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


def overall_success(problem_id: str) -> bool:
    return diagnosis_success(problem_id) and mitigation_success(problem_id)


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name.strip())
    return name[:180] if name else "report"


def _to_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def get_first(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def nested_get(d: Dict[str, Any], paths: List[List[str]]) -> Optional[Any]:
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


def is_event_record(rec: Dict[str, Any]) -> bool:
    return (
        rec.get("type") == "event"
        and isinstance(rec.get("stage"), str)
        and ("event_index" in rec)
    )


def detect_messages(rec: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """
    Your JSONL schema often has:
      - {"type":"event", ..., "messages":[{...}, ...], "last_message": {...}}
    """
    msgs = nested_get(rec, [["messages"], ["input", "messages"], ["output", "messages"]])
    if isinstance(msgs, list) and msgs and all(isinstance(m, dict) for m in msgs):
        return msgs
    return None


def detect_steps(rec: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    steps = get_first(rec, ["steps", "events", "trace", "spans"])
    if isinstance(steps, list) and steps and all(isinstance(s, dict) for s in steps):
        return steps
    return None


def last_message_preview(rec: Dict[str, Any], max_len: int = 160) -> str:
    """
    Prefer rec["last_message"], else fall back to the last item in messages.
    Returns: "<type>: <content-preview>"
    """
    lm = rec.get("last_message")
    if isinstance(lm, dict):
        t = as_str(lm.get("type") or lm.get("role") or "")
        c = lm.get("content")
        if isinstance(c, list):
            c_str = as_str(pretty_json(c), max_len=max_len)
        else:
            c_str = as_str(c, max_len=max_len)
        out = f"{t}: {c_str}".strip(": ").strip()
        return out

    msgs = detect_messages(rec)
    if msgs:
        last = msgs[-1]
        t = as_str(last.get("type") or last.get("role") or "")
        c = last.get("content")
        if isinstance(c, list):
            c_str = as_str(pretty_json(c), max_len=max_len)
        else:
            c_str = as_str(c, max_len=max_len)
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


def stream_pick_highest_event_index_per_stage(
    path: Path,
    stages_order: List[str],
) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """
    Stream the JSONL file; do NOT store all records.
    Keep ONLY the highest event_index event per target stage.
    """
    errors: List[str] = []
    total_lines = 0

    # best_num[stage] = (event_index_int, line_no, record)
    best_num: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}
    # best_fallback[stage] = (line_no, record)  # used only if no numeric seen
    best_fallback: Dict[str, Tuple[int, Dict[str, Any]]] = {}

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
            if stage not in stages_order:
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

    out: List[Dict[str, Any]] = []
    for s in stages_order:
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


def load_problem_index(jsonl_path: str) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                problem_id = obj.get("problem_id", "")
                if problem_id and problem_id not in index:
                    index[problem_id] = obj
            except Exception:
                continue
    return index


def load_attributes_index(root: Path) -> Dict[str, Dict[str, Any]]:
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
    overall_ok: str


@dataclass
class IndexRow:
    source_file: str
    link: str
    lines_scanned: int
    rendered: int
    parse_errors: int

    problem_id: str
    origin: str
    failure_type: str
    fault_level: str
    failure_level: str
    namespace: str
    application: str

    diagnosis_ok: str
    mitigation_ok: str
    overall_ok: str


_NS_RE = re.compile(
    r"It belongs to this namespace:\s*(?:\n\s*|\s+)([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

_APP_RE = re.compile(
    r"You will be working this application:\s*(?:\n\s*|\s+)([^\n\r]+)",
    re.IGNORECASE,
)


def find_namespace(rec: Dict[str, Any]) -> str:
    msgs = detect_messages(rec) or []
    for m in msgs:
        content = m.get("content", "")
        if isinstance(content, str):
            match = _NS_RE.search(content)
            if match:
                return match.group(1).strip()
    return ""


def find_application(rec: Dict[str, Any]) -> str:
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


def summarize_record(rec: Dict[str, Any], idx: int, file_problem_id: str) -> SummaryRow:
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
    ov_ok = False
    if problem_id:
        try:
            diag_ok = diagnosis_success(problem_id)
            mit_ok = mitigation_success(problem_id)
            ov_ok = diag_ok and mit_ok
        except Exception:
            diag_ok = False
            mit_ok = False
            ov_ok = False

    if problem_id and problem_id not in tags_by_problem_id:
        tags_by_problem_id[problem_id] = Tags(
            namespace=namespace,
            application=application,
            diagnosis_success=diag_ok,
            mitigation_success=mit_ok,
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
        overall_ok="true" if ov_ok else "false",
    )


def summarize_index_row(
    source_file: str,
    link: str,
    lines_scanned: int,
    rendered: int,
    parse_errors: int,
    problem_id: str,
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
        ov_ok = bool(t.overall_success)
    else:
        namespace = "default"
        application = "unknown"
        diag_ok = False
        mit_ok = False
        ov_ok = False

    return IndexRow(
        source_file=source_file,
        link=link,
        lines_scanned=lines_scanned,
        rendered=rendered,
        parse_errors=parse_errors,
        problem_id=problem_id,
        origin=origin,
        failure_type=failure_type,
        fault_level=fault_level,
        failure_level=failure_level,
        namespace=namespace,
        application=application,
        diagnosis_ok="true" if diag_ok else "false",
        mitigation_ok="true" if mit_ok else "false",
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
  <small>Generated {escape(now)} • Rendering ONLY highest event_index for stages: {escape(", ".join(TARGET_STAGES_ORDER))}</small>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def render_messages(msgs: List[Dict[str, Any]]) -> str:
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

        body_parts: List[str] = []

        if tool_calls:
            try:
                tool_calls_json = pretty_json(tool_calls)
            except Exception:
                tool_calls_json = json.dumps(tool_calls, ensure_ascii=False, indent=2)
            body_parts.append(
                "<div style='margin-top:6px;'>"
                "<div class='mono' style='color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em;'>tool_calls</div>"
                "<pre><code class='language-json'>"
                + escape(tool_calls_json)
                + "</code></pre>"
                "</div>"
            )

        if content_str.strip():
            content_div_cls = "content mono" if cls == "tool" else "content"
            body_parts.append(
                f"<div class='{content_div_cls}' style='white-space:pre-wrap'>{escape(content_str)}</div>"
            )

        if not body_parts:
            body_parts.append("<div class='content'><small>(empty)</small></div>")

        out.append(
            f"<div class='msg {cls}'>"
            f"<div class='role'>{escape(mtype)}</div>"
            + "\n".join(body_parts)
            + "</div>"
        )

    out.append("</div>")
    return "\n".join(out)


def render_kv(rec: Dict[str, Any], exclude_keys: set) -> str:
    items: List[Tuple[str, str]] = []
    for k, v in rec.items():
        if k in exclude_keys:
            continue
        if isinstance(v, (dict, list)):
            v_str = as_str(v, 300)
        else:
            v_str = as_str(v, 500)
        items.append((str(k), v_str))

    if not items:
        return ""

    html = [
        "<div class='card'><h3 style='margin:0 0 10px 0;'>Top-level fields</h3><div class='kv'>"
    ]
    for k, v in items[:60]:
        key_cls = "k hot" if k in HOT_KEYS else "k"
        html.append(
            f"<div><span class='{key_cls}'>{escape(k)}</span></div><div>{escape(v)}</div>"
        )

    if len(items) > 60:
        html.append(
            f"<div></div><div><small>+ {len(items)-60} more fields not shown</small></div>"
        )
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
    parts.append(chip("overall", r.overall_ok, "overall"))
    parts = [p for p in parts if p]
    if not parts:
        return "<small>(no tags)</small>"
    return "<div class='chips'>" + "\n".join(parts) + "</div>"


def render_file_report(
    file_name: str,
    records: List[Dict[str, Any]],
    parse_errors: List[str],
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

        parts.append("</div>")

        parts.append(
            "<div class='card'><details><summary>Raw JSON</summary>"
            "<pre><code class='language-json'>"
            + escape(pretty_json(rec))
            + "</code></pre>"
            "</details></div></div>"
        )

    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(
        description="Convert JSONL files to readable HTML reports (only highest event_index for target stages)."
    )
    ap.add_argument("inputs", nargs="+", help="Input .jsonl file(s) or directories containing .jsonl")
    ap.add_argument("-o", "--out", default="html_reports", help="Output directory")
    args = ap.parse_args()

    # Root selection: use the first input as the root if it's a directory,
    # otherwise use its parent directory.
    first = Path(args.inputs[0]).expanduser().resolve()
    root = first if first.is_dir() else first.parent

    # Load results.csv from the provided root (NOT the script directory)
    global all_results_csv
    results_csv_path = pick_results_csv_with_most_rows(root)
    all_results_csv = pd.read_csv(results_csv_path)
    pd.set_option("display.max_columns", None)

    # Load attributes.jsonl from the provided root (fallback to script dir)
    global ATTR_INDEX
    ATTR_INDEX = load_attributes_index(root)

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Run analysis report generation in the provided root so relative paths work
    generate_analysis_report(root)

    # Copy analysis_report.html from root into the output dir (if present)
    src = root / "analysis_report.html"
    destination = out_dir / "analysis_report.html"
    if src.exists():
        shutil.copy2(src, destination)

    jsonl_files: List[Path] = []
    for inp in args.inputs:
        p = Path(inp).expanduser().resolve()
        if p.is_dir():
            jsonl_files.extend(sorted(p.rglob("*.jsonl")))
        elif p.is_file() and p.suffix.lower() == ".jsonl":
            jsonl_files.append(p)
        else:
            print(f"Skipping (not .jsonl or dir): {p}")

    if not jsonl_files:
        raise SystemExit("No .jsonl files found.")

    index_rows: List[IndexRow] = []
    all_parse_errors: List[str] = []

    for fpath in jsonl_files:
        records, errors, total_lines = stream_pick_highest_event_index_per_stage(
            fpath, TARGET_STAGES_ORDER
        )
        file_pid = find_problem_id(fpath)
        all_parse_errors.extend(errors)

        base = safe_filename(fpath.stem)
        out_file = out_dir / f"{base}.html"

        body = render_file_report(fpath.name, records, errors, total_lines, file_problem_id=file_pid)
        html = html_page(f"{fpath.name} — Investigation Report", body)
        out_file.write_text(html, encoding="utf-8")

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
            )
        )

    idx = [
        "<div class='card'><h3 style='margin:0 0 10px 0;'>Reports</h3>",
        "<small>Click chips to filter. You can also use the dropdowns/search above.</small>",
        "</div>",
        INDEX_FILTER_UI,
        "<div class='card'>",
        "<table class='table'><thead><tr>"
        "<th>Source file</th><th>Problem</th><th>Tags</th><th>Lines scanned</th><th>Rendered events</th><th>Parse errors</th>"
        "</tr></thead><tbody>",
    ]

    for r in index_rows:
        search_blob = " | ".join(
            [
                r.source_file,
                r.link,
                r.problem_id,
                r.origin,
                r.failure_type,
                r.fault_level,
                r.failure_level,
                r.namespace,
                r.application,
                r.diagnosis_ok,
                r.mitigation_ok,
                r.overall_ok,
            ]
        )

        idx.append(
            "<tr "
            f"data-problem-id='{escape(r.problem_id)}' "
            f"data-origin='{escape(r.origin)}' "
            f"data-failure-type='{escape(r.failure_type)}' "
            f"data-fault-level='{escape(r.fault_level)}' "
            f"data-failure-level='{escape(r.failure_level)}' "
            f"data-namespace='{escape(r.namespace)}' "
            f"data-application='{escape(r.application)}' "
            f"data-diagnosis='{escape(r.diagnosis_ok)}' "
            f"data-mitigation='{escape(r.mitigation_ok)}' "
            f"data-overall='{escape(r.overall_ok)}' "
            f"data-search='{escape(search_blob)}'>"
            f"<td><a href='{escape(r.link)}'>{escape(r.source_file)}</a></td>"
            f"<td class='mono'>{escape(r.problem_id)}</td>"
            f"<td>{render_index_chips(r)}</td>"
            f"<td>{r.lines_scanned}</td>"
            f"<td>{r.rendered}</td>"
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