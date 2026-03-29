#!/usr/bin/env python3
"""
Summarize SREGym agent trajectory JSONL files using Claude.

Usage:
    uv run summarize_trajectory.py <file_or_dir> [options]

Examples:
    uv run summarize_trajectory.py logs/stratus/trajectory/0227_0755_*.jsonl
    uv run summarize_trajectory.py logs/stratus/trajectory/           # all files
    uv run summarize_trajectory.py my_run.jsonl --save                # writes .summary.md
    uv run summarize_trajectory.py my_run.jsonl --model claude-haiku-4-5-20251001
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Load .env from the repo root so ANTHROPIC_API_KEY is available without
# needing to export it in the shell every time.
def _load_dotenv(repo_root: Path) -> None:
    env_file = repo_root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)

_load_dotenv(Path(__file__).parent)

# ── Transcript extraction ──────────────────────────────────────────────────────

def _truncate(text: str, n: int) -> str:
    text = str(text or "").strip()
    return text[:n] + "…" if len(text) > n else text


def parse_trajectory(path: Path) -> dict:
    """
    Parse a trajectory JSONL into structured data.

    Returns:
        {
          "meta":   { problem_id, timestamp, total_events },
          "stages": [ { "name": str, "steps": [ {role, content, tool_name} ] } ]
        }

    Events within a stage are cumulative (each event carries the full history up
    to that point), so we only use the LAST event per stage and deduplicate
    messages that were already in a previous stage.
    """
    lines = path.read_text().splitlines()
    if not lines:
        return {"meta": {}, "stages": []}

    meta = {}
    # stage_name → list of event dicts, in order
    stage_buckets: dict[str, list] = {}
    stage_order: list[str] = []

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        t = obj.get("type")
        if t == "metadata":
            meta = {
                "problem_id": obj.get("problem_id", "unknown"),
                "timestamp":  obj.get("timestamp_readable", obj.get("timestamp", "")),
                "total_events": obj.get("total_events", 0),
            }
        elif t == "event":
            stage = obj.get("stage", "unknown")
            if stage not in stage_buckets:
                stage_buckets[stage] = []
                stage_order.append(stage)
            stage_buckets[stage].append(obj)

    # For each stage, take the last event (most complete message list).
    # Extract only the messages that are NEW relative to the previous stage's
    # final message list (tracked by content fingerprint).
    seen_fingerprints: set[str] = set()
    stages_out = []

    def _fingerprint(msg: dict) -> str:
        content = str(msg.get("content", ""))[:120]
        tc = msg.get("tool_calls", [])
        tc_sig = str(tc[0].get("name", "") if tc else "")
        return f"{msg.get('type','')[:3]}|{tc_sig}|{content}"

    for stage_name in stage_order:
        events = stage_buckets[stage_name]
        last_event = events[-1]
        messages = last_event.get("messages", [])
        submitted = last_event.get("submitted", False)

        steps = []
        for msg in messages:
            fp = _fingerprint(msg)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)

            mtype = msg.get("type", "")
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls", [])

            if mtype == "SystemMessage":
                continue  # skip — it's just the k8s primer, not interesting per run

            elif mtype == "HumanMessage":
                text = _truncate(content, 400)
                if text:
                    steps.append({"role": "context", "content": text})

            elif mtype == "AIMessage":
                if tool_calls:
                    for tc in tool_calls:
                        args_str = _truncate(json.dumps(tc.get("args", {})), 300)
                        steps.append({
                            "role": "tool_call",
                            "tool_name": tc.get("name", "?"),
                            "content": args_str,
                        })
                else:
                    text = _truncate(content, 600)
                    if text:
                        steps.append({"role": "reasoning", "content": text})

            elif mtype == "ToolMessage":
                steps.append({"role": "tool_result", "content": _truncate(content, 500)})

        stages_out.append({
            "name": stage_name,
            "steps": steps,
            "submitted": submitted,
            "num_events": len(events),
        })

    return {"meta": meta, "stages": stages_out}


def build_transcript(data: dict, max_chars: int = 30_000) -> str:
    """Render parsed trajectory as a readable plain-text transcript."""
    meta = data["meta"]
    lines = [
        f"Problem:   {meta.get('problem_id', 'unknown')}",
        f"Timestamp: {meta.get('timestamp', '')}",
        f"Events:    {meta.get('total_events', '?')}",
        f"Stages:    {len(data['stages'])}",
        "",
    ]

    for stage in data["stages"]:
        lines.append(f"═══ Stage: {stage['name']}  ({stage['num_events']} events) ═══")
        for step in stage["steps"]:
            role = step["role"]
            if role == "context":
                lines.append(f"[CONTEXT] {step['content']}")
            elif role == "reasoning":
                lines.append(f"[THINK]   {step['content']}")
            elif role == "tool_call":
                lines.append(f"[CALL]    {step['tool_name']}({step['content']})")
            elif role == "tool_result":
                lines.append(f"[RESULT]  {step['content']}")
        if stage["submitted"]:
            lines.append("[SUBMITTED ✓]")
        lines.append("")

    transcript = "\n".join(lines)
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + f"\n… [truncated at {max_chars} chars]"
    return transcript


# ── HTML rendering ────────────────────────────────────────────────────────────

def summary_to_html(summary_md: str, title: str) -> str:
    """Convert a markdown summary to a self-contained styled HTML page."""
    import html as html_mod
    import re

    lines = summary_md.splitlines()
    body_parts = []
    in_ul = False

    for line in lines:
        stripped = line.strip()

        # Close open list if needed
        if in_ul and not stripped.startswith("- "):
            body_parts.append("</ul>")
            in_ul = False

        if stripped.startswith("## "):
            body_parts.append(f'<h2>{html_mod.escape(stripped[3:])}</h2>')
        elif stripped.startswith("# "):
            body_parts.append(f'<h1>{html_mod.escape(stripped[2:])}</h1>')
        elif stripped.startswith("- "):
            if not in_ul:
                body_parts.append("<ul>")
                in_ul = True
            content = html_mod.escape(stripped[2:])
            # bold
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            body_parts.append(f"<li>{content}</li>")
        elif stripped == "":
            body_parts.append("<br>")
        else:
            content = html_mod.escape(stripped)
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            content = re.sub(r'`(.+?)`', r'<code>\1</code>', content)
            body_parts.append(f"<p>{content}</p>")

    if in_ul:
        body_parts.append("</ul>")

    body_html = "\n".join(body_parts)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>{html_mod.escape(title)}</title>
<style>
body {{
    margin: 0;
    background: #0f172a;
    color: #e2e8f0;
    font-family: Consolas, monospace;
    padding: 30px 50px;
    max-width: 860px;
}}
h1 {{ color: #a0c4ff; border-bottom: 1px solid #334155; padding-bottom: 8px; }}
h2 {{ color: #7dd3fc; margin-top: 28px; margin-bottom: 8px; }}
p {{ margin: 6px 0; line-height: 1.6; }}
ul {{ margin: 6px 0 6px 20px; padding: 0; }}
li {{ margin: 4px 0; line-height: 1.6; }}
strong {{ color: #f8fafc; }}
code {{
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 0.9em;
    color: #93c5fd;
}}
.filename {{
    font-size: 0.8em;
    color: #64748b;
    margin-bottom: 24px;
}}
</style>
</head>
<body>
<h1>Trajectory Summary</h1>
<div class="filename">{html_mod.escape(title)}</div>
{body_html}
</body>
</html>"""


# ── LLM summarization ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert SRE analyst reviewing Kubernetes fault-injection benchmark runs.
You will receive a transcript of an AI agent's trajectory as it investigated and
tried to fix a fault. Produce a concise, structured summary."""

SUMMARY_PROMPT = """\
Below is the full trajectory of an SRE benchmark agent run.

{transcript}

---

Write a summary with these sections:

## Problem
One sentence: what fault was injected, in which application/namespace.

## Investigation
Bullet list of the main things the agent checked (tools called, key observations).
Keep each bullet to one line. Skip redundant or repeated checks.

## Diagnosis
What root cause did the agent identify? Was it correct? (If a submission is visible, note it.)

## Mitigation
What fix(es) did the agent attempt? Did any succeed?
If no mitigation stage, say "None attempted."

## Outcome
- Diagnosis: success / failure / unclear
- Mitigation: success / failure / not attempted
- Notable: any interesting patterns, mistakes, or efficient strategies (1–3 bullets max).
"""


def summarize_with_llm(transcript: str, model: str) -> str:
    """Call the project LLM backend to produce the structured summary."""
    from langchain_core.messages import HumanMessage
    from llm_backend.init_backend import get_llm_backend

    backend = get_llm_backend(model)
    result = backend.inference(
        messages=[HumanMessage(content=SUMMARY_PROMPT.format(transcript=transcript))],
        system_prompt=SYSTEM_PROMPT,
    )
    return result.content


# ── CLI ───────────────────────────────────────────────────────────────────────

def collect_files(target: str) -> list[Path]:
    p = Path(target)
    if p.is_dir():
        # Match both naming patterns:
        #   old: 0113_HHMM_problem_stratus_agent_trajectory.jsonl
        #   new: stratus_agent_trajectory_problem_YYYYMMDD_HHMMSS.jsonl
        files = set(p.glob("*_trajectory.jsonl")) | set(p.glob("stratus_agent_trajectory_*.jsonl"))
        return sorted(files)
    if p.is_file():
        return [p]
    parent = p.parent
    return sorted(parent.glob(p.name))


def _problem_id_from_filename(path: Path) -> str | None:
    """Extract problem_id from the new filename pattern.

    stratus_agent_trajectory_PROBLEM_YYYYMMDD_HHMMSS.jsonl
    → PROBLEM (may contain underscores; strip trailing date/time parts)
    """
    import re
    m = re.match(r"stratus_agent_trajectory_(.+)_\d{8}_\d{6}\.jsonl$", path.name)
    if m:
        return m.group(1)
    return None


def group_by_problem(files: list[Path]) -> dict[str, list[Path]]:
    """Read the metadata line of each file and group paths by problem_id."""
    groups: dict[str, list[Path]] = {}
    for path in files:
        pid = None
        try:
            first_line = path.open().readline()
            meta = json.loads(first_line)
            pid = meta.get("problem_id") or None
        except Exception:
            pass
        if not pid:
            pid = _problem_id_from_filename(path) or "unknown"
        groups.setdefault(pid, []).append(path)
    return groups


def generate_index_html(all_runs: dict[str, list[dict]]) -> str:
    """Generate a single HTML file with a sidebar index of all problems.

    all_runs: { problem_id: [ {timestamp, filename, summary_md}, ... ] }
    """
    import html as html_mod
    import re

    def md_fragment(md: str) -> str:
        lines = md.splitlines()
        parts = []
        in_ul = False
        for line in lines:
            s = line.strip()
            if in_ul and not s.startswith("- "):
                parts.append("</ul>")
                in_ul = False
            if s.startswith("## "):
                parts.append(f'<h4>{html_mod.escape(s[3:])}</h4>')
            elif s.startswith("- "):
                if not in_ul:
                    parts.append("<ul>")
                    in_ul = True
                c = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_mod.escape(s[2:]))
                parts.append(f"<li>{c}</li>")
            elif s == "":
                pass
            else:
                c = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>',
                           re.sub(r'`(.+?)`', r'<code>\1</code>', html_mod.escape(s)))
                parts.append(f"<p>{c}</p>")
        if in_ul:
            parts.append("</ul>")
        return "\n".join(parts)

    def slug(pid: str) -> str:
        return pid.replace(" ", "-").replace("/", "-")

    # Sidebar: one entry per problem
    sidebar_items = ""
    for pid, runs in sorted(all_runs.items()):
        sidebar_items += f"""
        <a href="#{slug(pid)}" class="prob-link">
          <span class="prob-name">{html_mod.escape(pid)}</span>
          <span class="prob-count">{len(runs)}</span>
        </a>"""

    # Main content: one section per problem, run cards inside
    sections = ""
    for pid, runs in sorted(all_runs.items()):
        run_cards = ""
        for r in runs:
            run_cards += f"""
            <div class="run-card">
              <div class="run-header">
                <span class="run-ts">{html_mod.escape(r["timestamp"])}</span>
                <span class="run-file">{html_mod.escape(r["filename"])}</span>
              </div>
              {md_fragment(r["summary_md"])}
            </div>"""

        sections += f"""
        <section class="problem-section" id="{slug(pid)}">
          <h2 class="prob-title">{html_mod.escape(pid)}
            <span class="run-count">{len(runs)} run{"s" if len(runs) != 1 else ""}</span>
          </h2>
          {run_cards}
        </section>"""

    total = sum(len(v) for v in all_runs.values())

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>SREGym Trajectory Summaries</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    background: #0f172a;
    color: #e2e8f0;
    font-family: Consolas, monospace;
    display: flex;
    min-height: 100vh;
}}
/* ── Sidebar ── */
.sidebar {{
    width: 260px;
    min-width: 260px;
    background: #1e293b;
    border-right: 1px solid #334155;
    padding: 22px 14px;
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: #334155 transparent;
}}
.sidebar-title {{
    font-size: 0.95em;
    font-weight: 700;
    color: #a0c4ff;
    margin-bottom: 4px;
}}
.sidebar-sub {{
    font-size: 0.7em;
    color: #475569;
    margin-bottom: 16px;
}}
.prob-link {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 8px;
    border-radius: 5px;
    text-decoration: none;
    margin-bottom: 2px;
    border-left: 2px solid transparent;
    transition: background 0.1s;
}}
.prob-link:hover {{
    background: #334155;
    border-left-color: #7dd3fc;
}}
.prob-name {{
    font-size: 0.72em;
    color: #94a3b8;
    word-break: break-all;
    line-height: 1.4;
}}
.prob-link:hover .prob-name {{ color: #e2e8f0; }}
.prob-count {{
    font-size: 0.68em;
    color: #475569;
    background: #0f172a;
    border-radius: 10px;
    padding: 1px 6px;
    margin-left: 6px;
    flex-shrink: 0;
}}
/* ── Main ── */
.main {{
    flex: 1;
    padding: 36px 52px;
    overflow-y: auto;
    max-width: 960px;
}}
.page-title {{
    font-size: 1.4em;
    font-weight: 700;
    color: #a0c4ff;
    border-bottom: 1px solid #334155;
    padding-bottom: 10px;
    margin-bottom: 32px;
}}
.page-title span {{ font-size: 0.55em; color: #475569; margin-left: 12px; }}
.problem-section {{
    margin-bottom: 48px;
    scroll-margin-top: 24px;
}}
.prob-title {{
    font-size: 1em;
    color: #7dd3fc;
    margin-bottom: 14px;
    padding-bottom: 7px;
    border-bottom: 1px solid #1e3a5f;
}}
.run-count {{ font-size: 0.65em; color: #475569; margin-left: 10px; font-weight: 400; }}
.run-card {{
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 14px;
}}
.run-header {{
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid #2d3f55;
}}
.run-ts {{ color: #38bdf8; font-size: 0.85em; font-weight: 700; }}
.run-file {{ font-size: 0.68em; color: #475569; }}
h4 {{ color: #93c5fd; margin: 12px 0 4px; font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.1em; }}
p {{ margin: 4px 0; line-height: 1.6; font-size: 0.85em; }}
ul {{ margin: 4px 0 4px 16px; }}
li {{ margin: 3px 0; line-height: 1.55; font-size: 0.85em; }}
strong {{ color: #f1f5f9; }}
code {{
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 3px;
    padding: 1px 4px;
    font-size: 0.82em;
    color: #93c5fd;
}}
</style>
</head>
<body>
<div class="sidebar">
  <div class="sidebar-title">⬡ SREGym</div>
  <div class="sidebar-sub">{len(all_runs)} problems · {total} runs</div>
  {sidebar_items}
</div>
<div class="main">
  <div class="page-title">
    Trajectory Summaries
    <span>{len(all_runs)} problems · {total} runs</span>
  </div>
  {sections}
</div>
</body>
</html>"""


def summarize_file(path: Path, model: str, verbose: bool) -> str | None:
    """Summarize one trajectory file, return the markdown summary or None on error."""
    data = parse_trajectory(path)
    if not data["stages"]:
        print(f"  (no events — skipping {path.name})")
        return None

    transcript = build_transcript(data)
    if verbose:
        print("\n--- TRANSCRIPT ---")
        print(transcript)
        print("--- END TRANSCRIPT ---\n")

    try:
        return summarize_with_llm(transcript, model)
    except Exception as e:
        msg = str(e)
        if "auth" in msg.lower() or "api_key" in msg.lower() or "401" in msg:
            print(f"  AUTH ERROR: {msg}\n  Set OPENAI_API_KEY / GEMINI_API_KEY in .env or shell.")
        else:
            print(f"  ERROR: {msg}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize SREGym agent trajectory JSONL files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="JSONL file, directory, or glob pattern")
    parser.add_argument(
        "--model", "-m",
        default="gpt-4o",
        help="Model ID from llm_backend/configs.yaml (default: gpt-4o)",
    )
    parser.add_argument(
        "--problem", "-p",
        default=None,
        help="Only process trajectories for this problem_id",
    )
    parser.add_argument(
        "--save", "-s",
        action="store_true",
        help="Save each summary as <trajectory>.summary.md",
    )
    parser.add_argument(
        "--save-html",
        action="store_true",
        help="Save one HTML per problem grouping all its run summaries",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory to write HTML files into (default: next to the trajectory files)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print the extracted transcript before summarizing",
    )
    args = parser.parse_args()

    files = collect_files(args.target)
    if not files:
        print(f"No trajectory files found at: {args.target}", file=sys.stderr)
        sys.exit(1)

    # Group by problem, then optionally filter to one problem
    groups = group_by_problem(files)
    if args.problem:
        if args.problem not in groups:
            available = "\n  ".join(sorted(groups))
            print(f"Problem '{args.problem}' not found. Available:\n  {available}", file=sys.stderr)
            sys.exit(1)
        groups = {args.problem: groups[args.problem]}

    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    total_files = sum(len(v) for v in groups.values())
    print(f"Found {total_files} file(s) across {len(groups)} problem(s)  |  model: {args.model}")

    all_runs: dict[str, list[dict]] = {}

    for problem_id, problem_files in sorted(groups.items()):
        print(f"\n{'═'*60}")
        print(f"Problem: {problem_id}  ({len(problem_files)} run(s))")
        print(f"{'═'*60}")

        runs = []
        for path in problem_files:
            print(f"\n  [{path.name}]")
            meta = parse_trajectory(path).get("meta", {})
            summary = summarize_file(path, model=args.model, verbose=args.verbose)
            if summary is None:
                continue

            print(summary)

            if args.save:
                out = (out_dir or path.parent) / path.with_suffix(".summary.md").name
                out.write_text(f"# {problem_id}\n\n**File:** `{path.name}`\n\n" + summary)
                print(f"  Saved → {out}")

            runs.append({
                "timestamp": meta.get("timestamp", path.name[:13]),
                "filename":  path.name,
                "summary_md": summary,
            })

        if runs:
            all_runs[problem_id] = runs

    if args.save_html and all_runs:
        base = Path(args.target) if Path(args.target).is_dir() else Path(args.target).parent
        out = (out_dir or base) / "trajectories_index.html"
        out.write_text(generate_index_html(all_runs))
        print(f"\n  Index HTML → {out}")

    print(f"\n{'═'*60}")
    print(f"Done. {total_files} file(s) / {len(groups)} problem(s) processed.")


if __name__ == "__main__":
    main()
