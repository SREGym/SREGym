#!/usr/bin/env python3
import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Keep ONLY the single highest-event_index "event" record per stage (per file),
# but render the FULL event using your existing HTML logic.
TARGET_STAGES_ORDER = ["diagnosis", "mitigation_attempt_0"]

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
    return rec.get("type") == "event" and isinstance(rec.get("stage"), str) and ("event_index" in rec)


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


# ---------- streaming selector (no full-file load) ----------


def stream_pick_highest_event_index_per_stage(
    path: Path,
    stages_order: List[str],
) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """
    Stream the JSONL file; do NOT store all records.
    Keep ONLY the highest event_index event per target stage.

    Selection:
      - Only considers dict records where type=="event" and stage in stages_order.
      - Highest numeric event_index wins.
      - If event_index is missing/non-numeric for some records, they are kept only
        if we never saw a numeric event_index for that stage (fallback to latest seen).
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
                # only used as fallback if we never see numeric for that stage
                prev = best_fallback.get(stage)
                if prev is None or line_no > prev[0]:
                    best_fallback[stage] = (line_no, obj)
                continue

            prev = best_num.get(stage)
            if prev is None:
                best_num[stage] = (ei_int, line_no, obj)
            else:
                cur_ei, cur_ln, _ = prev
                # strictly higher event_index wins; tie-break by later line
                if (ei_int > cur_ei) or (ei_int == cur_ei and line_no > cur_ln):
                    best_num[stage] = (ei_int, line_no, obj)

    out: List[Dict[str, Any]] = []
    for s in stages_order:
        if s in best_num:
            out.append(best_num[s][2])
        elif s in best_fallback:
            out.append(best_fallback[s][1])

    return out, errors, total_lines


# ----------------------------
# Summary dataclass
# ----------------------------


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


def summarize_record(rec: Dict[str, Any], idx: int) -> SummaryRow:
    rec_type = as_str(rec.get("type"))
    stage = as_str(rec.get("stage"))
    event_index = as_str(rec.get("event_index"))
    submitted = as_str(rec.get("submitted"))
    num_steps = as_str(rec.get("num_steps"))
    problem_id = as_str(rec.get("problem_id"))
    timestamp = as_str(rec.get("timestamp_readable") or rec.get("timestamp"))

    return SummaryRow(
        idx=idx,
        rec_type=rec_type,
        stage=stage,
        event_index=event_index,
        submitted=submitted,
        num_steps=num_steps,
        problem_id=problem_id,
        timestamp=timestamp,
    )


# ----------------------------
# HTML Templates
# ----------------------------

HIGHLIGHT = """
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>window.addEventListener('load', () => hljs.highlightAll());</script>
"""

BASE_CSS = """
<style>
:root { --bg:#ffffff; --fg:#111; --muted:#666; --card:#f7f7f9; --border:#e6e6ea; }
* { box-sizing: border-box; }
body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; color: var(--fg); background: var(--bg); }
header { padding: 18px 22px; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: rgba(255,255,255,0.92); backdrop-filter: blur(6px); }
h1 { margin: 0; font-size: 18px; }
small { color: var(--muted); }
main { padding: 18px 22px; max-width: 1200px; margin: 0 auto; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin: 12px 0; }
.table { width: 100%; border-collapse: collapse; }
.table th, .table td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--border); vertical-align: top; font-size: 13px; }
.table th { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.badge { display: inline-block; padding: 2px 8px; border: 1px solid var(--border); border-radius: 999px; font-size: 12px; margin-right: 6px; background: #fff; }
a { color: #0b5fff; text-decoration: none; }
a:hover { text-decoration: underline; }
details > summary { cursor: pointer; color: var(--muted); }
/* --- code blocks: wrap instead of horizontal scroll --- */
pre {
  overflow-x: auto;          /* keep as a fallback */
  white-space: pre-wrap;     /* wrap long lines */
  word-break: break-word;    /* break long tokens */
  overflow-wrap: anywhere;   /* allow breaks anywhere if needed */
}

pre code {
  white-space: pre-wrap;     /* wrap inside highlight.js code blocks */
  word-break: break-word;
  overflow-wrap: anywhere;
}

/* --- message content: wrap long strings/JSON nicely --- */
.msg .content {
  white-space: pre-wrap;     /* preserve newlines, wrap long lines */
  word-break: break-word;
  overflow-wrap: anywhere;
}

.grid { display: grid; grid-template-columns: 1fr; gap: 10px; }

/* was: 1fr 1fr */
@media (min-width: 900px) {
  .grid {
    grid-template-columns: minmax(260px, 360px) 1fr;
    align-items: start;
  }
}


.msg { border: 1px solid var(--border); border-radius: 12px; padding: 10px 12px; background: #fff; margin-bottom: 10px; }
.msg .role { font-size: 12px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: .04em; }
.msg.user { border-left: 5px solid #0b5fff22; }
.msg.assistant { border-left: 5px solid #16a34a22; }
.msg.tool { border-left: 5px solid #f59e0b22; }
.kv { display: grid; grid-template-columns: 170px 1fr; gap: 6px 12px; font-size: 13px; }
hr { border: 0; border-top: 1px solid var(--border); margin: 18px 0; }

/* --- monospace helpers --- */
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
.msg.tool, .msg.tool .content, .msg.tool pre, .msg.tool code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }

/* make tool messages visually distinct */
.msg.tool { background: #fffdf5; }

/* --- highlight important KV keys --- */
.kv .k { color: var(--muted); }
.kv .k.hot {
  color: var(--fg);
  font-weight: 650;
  background: #ffffff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 2px 8px;
  display: inline-block;
}

/* badges stronger emphasis */
.badge.hot {
  border-color: #0b5fff55;
  background: #0b5fff0a;
  font-weight: 650;
}
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

        out.append(
            f"<div class='msg {cls}'>" f"<div class='role'>{escape(mtype)}</div>" + "\n".join(body_parts) + "</div>"
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

    html = ["<div class='card'><h3 style='margin:0 0 10px 0;'>Top-level fields</h3><div class='kv'>"]
    for k, v in items[:60]:
        key_cls = "k hot" if k in HOT_KEYS else "k"
        html.append(f"<div><span class='{key_cls}'>{escape(k)}</span></div><div>{escape(v)}</div>")

    if len(items) > 60:
        html.append(f"<div></div><div><small>+ {len(items)-60} more fields not shown</small></div>")
    html.append("</div></div>")
    return "\n".join(html)


def render_file_report(
    file_name: str, records: List[Dict[str, Any]], parse_errors: List[str], total_lines_scanned: int
) -> str:
    # records will be only the selected events now (<=2)
    rows = [summarize_record(r, i + 1) for i, r in enumerate(records)]

    event_mode = False
    if records:
        event_hits = sum(1 for r in records if is_event_record(r))
        event_mode = event_hits >= max(1, int(0.6 * len(records)))

    # Timeline table (still useful even with only 1–2 records)
    if event_mode:
        table = [
            "<div class='card'>",
            "<h3 style='margin:0 0 10px 0;'>Investigation Timeline</h3>",
            f"<small>Source: <span class='mono'>{escape(file_name)}</span> • Scanned {total_lines_scanned} lines • Rendered {len(records)} event(s)</small>",
            "<div style='height:10px'></div>",
            "<table class='table'>",
            "<thead><tr>"
            "<th>#</th><th>Stage</th><th>Event #</th><th>Submitted</th><th>Steps</th><th>Last message</th><th>Problem</th><th>Timestamp</th>"
            "</tr></thead><tbody>",
        ]
        for r in rows:
            anchor = f"evt-{r.idx}"
            lm = last_message_preview(records[r.idx - 1])
            table.append(
                "<tr>"
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
            "<table class='table'>",
            "<thead><tr>"
            "<th>#</th><th>Type</th><th>Stage</th><th>Event #</th><th>Submitted</th><th>Steps</th><th>Problem</th><th>Timestamp</th>"
            "</tr></thead><tbody>",
        ]
        for r in rows:
            anchor = f"evt-{r.idx}"
            table.append(
                "<tr>"
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

    # Full per-record rendering (your original logic)
    for i, rec in enumerate(records, start=1):
        s = summarize_record(rec, i)
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
            "<pre><code class='language-json'>" + escape(pretty_json(rec)) + "</code></pre>"
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

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    jsonl_files: List[Path] = []
    for inp in args.inputs:
        p = Path(inp)
        if p.is_dir():
            jsonl_files.extend(sorted(p.rglob("*.jsonl")))
        elif p.is_file() and p.suffix.lower() == ".jsonl":
            jsonl_files.append(p)
        else:
            print(f"Skipping (not .jsonl or dir): {p}")

    if not jsonl_files:
        raise SystemExit("No .jsonl files found.")

    # index rows: (src, link, lines_scanned, rendered_events, parse_errors)
    index_rows: List[Tuple[str, str, int, int, int]] = []
    all_parse_errors: List[str] = []

    for fpath in jsonl_files:
        records, errors, total_lines = stream_pick_highest_event_index_per_stage(fpath, TARGET_STAGES_ORDER)
        all_parse_errors.extend(errors)

        base = safe_filename(fpath.stem)
        out_file = out_dir / f"{base}.html"

        body = render_file_report(fpath.name, records, errors, total_lines)
        html = html_page(f"{fpath.name} — Investigation Report", body)
        out_file.write_text(html, encoding="utf-8")

        index_rows.append((fpath.name, out_file.name, total_lines, len(records), len(errors)))

    # index.html
    idx = [
        "<div class='card'><h3 style='margin:0 0 10px 0;'>Reports</h3>",
        "<table class='table'><thead><tr>"
        "<th>Source file</th><th>Lines scanned</th><th>Rendered events</th><th>Parse errors</th>"
        "</tr></thead><tbody>",
    ]
    for src, link, lines_scanned, rendered, errc in index_rows:
        idx.append(
            "<tr>"
            f"<td><a href='{escape(link)}'>{escape(src)}</a></td>"
            f"<td>{lines_scanned}</td>"
            f"<td>{rendered}</td>"
            f"<td>{errc}</td>"
            "</tr>"
        )
    idx.append("</tbody></table></div>")

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
