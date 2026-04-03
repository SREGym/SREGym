"""
SREGym Leaderboard Generator
=============================
Reads all agent result CSVs and generates a self-contained leaderboard.html.

Internal results:  ../  (*_ALL_results.csv, agent name parsed from filename)
External results:  submissions/<agent_name>/*.csv  (folder name = agent name)

Usage:
    # Generate HTML leaderboard
    python leaderboard/leaderboard.py

    # Preview YOUR results privately in the terminal (not submitted to leaderboard)
    python leaderboard/leaderboard.py preview --agent ciroos --csv /path/to/results.csv
    python leaderboard/leaderboard.py preview --agent ciroos --csv /path/to/results_dir/
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Category mapping (derived from registry.py)
# ---------------------------------------------------------------------------
CATEGORY_RULES = [
    ("K8s Operator",       lambda pid: pid.startswith("operator_")),
    ("Hardware Faults",    lambda pid: pid in {"silent_data_corruption", "latent_sector_error", "read_error"}),
    ("Train Ticket",       lambda pid: pid.startswith("trainticket_")),
    ("OpenTelemetry",      lambda pid: pid.startswith("astronomy_shop_") or pid in {"kafka_queue_problems", "loadgenerator_flood_homepage"}),
    ("Metastable",         lambda pid: pid in {"capacity_decrease_rpc_retry_storm", "gc_capacity_degradation", "load_spike_rpc_retry_storm"}),
    ("Correlated Faults",  lambda pid: pid.endswith("_correlated")),
    ("Application Faults", lambda pid: any(pid.startswith(p) for p in (
        "incorrect_", "missing_env_variable", "revoke_auth_", "storage_user_",
        "valkey_", "misconfig_app", "auth_miss_mongodb",
    ))),
    ("K8s Config",         lambda pid: True),  # catch-all
]

def get_category(problem_id: str) -> str:
    for name, rule in CATEGORY_RULES:
        if rule(problem_id):
            return name
    return "Other"


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------
def parse_bool(val) -> bool | None:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


def load_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def agent_from_filename(filename: str) -> str | None:
    """
    Extract agent name from internal result CSV filenames.
    Patterns:
      0216_1502_namespace_memory_limit_stratus_results.csv  -> stratus
      02-16_14-30_autosubmit_ALL_results.csv                -> autosubmit
      MMDD_HHMM_<problem>_<agent>_results.csv               -> <agent>
    """
    name = Path(filename).stem  # strip .csv
    # Remove trailing _results
    name = re.sub(r"_results$", "", name)
    # Try pattern: MMDD_HHMM_..._AGENT  (last segment after removing timestamp)
    parts = re.split(r"[_\-]", name)
    # Known agent names
    known_agents = {"stratus", "autosubmit", "codex", "claudecode", "gemini", "opencode", "resolve", "demo"}
    # Walk from end to find a known agent token
    for part in reversed(parts):
        if part.lower() in known_agents:
            return part.lower()
    # If _ALL_ is in the name, try the segment before _ALL_
    m = re.search(r"_([a-zA-Z]+)_ALL", name)
    if m:
        return m.group(1).lower()
    return None


def load_all_results(root: Path) -> dict[str, list[dict]]:
    """
    Returns: { agent_name: [row, row, ...] }
    Each row guaranteed to have: problem_id, diag_success, mitig_success, ttl, ttm, agent
    """
    agent_rows: dict[str, list[dict]] = defaultdict(list)

    # --- Internal: *_ALL_results.csv in root ---
    for csv_path in sorted(root.glob("*_ALL_results.csv")):
        agent = agent_from_filename(csv_path.name)
        if agent is None:
            continue
        for row in load_csv(csv_path):
            pid = row.get("problem_id", "").strip()
            if not pid:
                continue
            diag = parse_bool(row.get("Diagnosis.success"))
            mitig = parse_bool(row.get("Mitigation.success"))
            ttl = _float(row.get("TTL"))
            ttm = _float(row.get("TTM"))
            agent_rows[agent].append({
                "problem_id": pid,
                "diag_success": diag,
                "mitig_success": mitig,
                "ttl": ttl,
                "ttm": ttm,
                "agent": agent,
            })

    # --- External: submissions/<agent_name>/*.csv ---
    submissions_dir = root / "leaderboard" / "submissions"
    if submissions_dir.exists():
        for agent_dir in sorted(submissions_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent = agent_dir.name.lower()
            for csv_path in sorted(agent_dir.glob("*.csv")):
                for row in load_csv(csv_path):
                    pid = row.get("problem_id", "").strip()
                    if not pid:
                        continue
                    diag = parse_bool(row.get("Diagnosis.success"))
                    mitig = parse_bool(row.get("Mitigation.success"))
                    ttl = _float(row.get("TTL"))
                    ttm = _float(row.get("TTM"))
                    agent_rows[agent].append({
                        "problem_id": pid,
                        "diag_success": diag,
                        "mitig_success": mitig,
                        "ttl": ttl,
                        "ttm": ttm,
                        "agent": agent,
                    })

    return dict(agent_rows)


def load_mock_agents(root: Path) -> set[str]:
    """Read submissions/mock_agents.txt — one agent name per line."""
    path = root / "leaderboard" / "submissions" / "mock_agents.txt"
    if not path.exists():
        return set()
    lines = path.read_text().splitlines()
    return {l.strip().lower() for l in lines if l.strip() and not l.startswith("#")}


def _float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def compute_agent_stats(rows: list[dict]) -> dict:
    """Overall and per-category stats for one agent."""
    problems_seen: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        problems_seen[row["problem_id"]].append(row)

    # For each problem take the BEST attempt (if multiple)
    best: list[dict] = []
    for pid, attempts in problems_seen.items():
        # prefer a run where diag is True, then mitig is True
        def score(r):
            return (int(r["diag_success"] or False), int(r["mitig_success"] or False))
        best.append(max(attempts, key=score))

    total = len(best)
    diag_pass = sum(1 for r in best if r["diag_success"])
    mitig_pass = sum(1 for r in best if r["mitig_success"])
    full_pass = sum(1 for r in best if r["diag_success"] and (r["mitig_success"] is not False))

    ttls = [r["ttl"] for r in best if r["ttl"] is not None and r["diag_success"]]
    ttms = [r["ttm"] for r in best if r["ttm"] is not None and r["mitig_success"]]

    # Per-category
    cat_stats: dict[str, dict] = {}
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in best:
        by_cat[get_category(row["problem_id"])].append(row)
    for cat, cat_rows in by_cat.items():
        n = len(cat_rows)
        d = sum(1 for r in cat_rows if r["diag_success"])
        m = sum(1 for r in cat_rows if r["mitig_success"])
        cat_stats[cat] = {"total": n, "diag": d, "mitig": m, "score": round(d / n * 100) if n else 0}

    # Per-problem pass/fail for the heatmap
    problem_results = {
        r["problem_id"]: {
            "diag": r["diag_success"],
            "mitig": r["mitig_success"],
            "category": get_category(r["problem_id"]),
        }
        for r in best
    }

    return {
        "total": total,
        "diag_pass": diag_pass,
        "mitig_pass": mitig_pass,
        "full_pass": full_pass,
        "diag_pct": round(diag_pass / total * 100, 1) if total else 0,
        "mitig_pct": round(mitig_pass / total * 100, 1) if total else 0,
        "score_pct": round(diag_pass / total * 100, 1) if total else 0,  # primary ranking metric
        "avg_ttl": round(sum(ttls) / len(ttls)) if ttls else None,
        "avg_ttm": round(sum(ttms) / len(ttms)) if ttms else None,
        "categories": cat_stats,
        "problems": problem_results,
    }


def build_leaderboard(agent_rows: dict[str, list[dict]]) -> list[dict]:
    """Returns sorted list of {agent, stats} dicts."""
    entries = []
    for agent, rows in agent_rows.items():
        stats = compute_agent_stats(rows)
        entries.append({"agent": agent, "stats": stats})
    entries.sort(key=lambda e: (-e["stats"]["score_pct"], -e["stats"]["total"]))
    for i, entry in enumerate(entries):
        entry["rank"] = i + 1
    return entries


# ---------------------------------------------------------------------------
# HTML Generation
# ---------------------------------------------------------------------------
CATEGORY_COLORS = {
    "K8s Config":        {"hex": "#4f8ef7", "glow": "rgba(79,142,247,0.4)"},
    "Application Faults":{"hex": "#f7a24f", "glow": "rgba(247,162,79,0.4)"},
    "K8s Operator":      {"hex": "#c084fc", "glow": "rgba(192,132,252,0.4)"},
    "OpenTelemetry":     {"hex": "#34d399", "glow": "rgba(52,211,153,0.4)"},
    "Metastable":        {"hex": "#f87171", "glow": "rgba(248,113,113,0.4)"},
    "Correlated Faults": {"hex": "#fbbf24", "glow": "rgba(251,191,36,0.4)"},
    "Hardware Faults":   {"hex": "#22d3ee", "glow": "rgba(34,211,238,0.4)"},
    "Train Ticket":      {"hex": "#f472b6", "glow": "rgba(244,114,182,0.4)"},
    "Other":             {"hex": "#9ca3af", "glow": "rgba(156,163,175,0.4)"},
}

RANK_CONFIG = {
    1: {"icon": "1ST", "color": "#FFD700", "glow": "rgba(255,215,0,0.3)",  "label": "GOLD"},
    2: {"icon": "2ND", "color": "#C0C0C0", "glow": "rgba(192,192,192,0.2)", "label": "SILVER"},
    3: {"icon": "3RD", "color": "#CD7F32", "glow": "rgba(205,127,50,0.2)",  "label": "BRONZE"},
}

def fmt_time(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"

def agent_initials(name: str) -> str:
    parts = name.replace("-", " ").replace("_", " ").split()
    return "".join(p[0].upper() for p in parts[:2]) if len(parts) >= 2 else name[:2].upper()

def render_html(leaderboard: list[dict], all_problems: set[str], mock_agents: set[str] = None) -> str:
    mock_agents = mock_agents or set()
    all_categories = sorted(set(get_category(p) for p in all_problems))
    n_agents = len(leaderboard)
    n_problems = len(all_problems)

    # --- Podium (top 3) ---
    podium_html = ""
    podium_order = []
    if n_agents >= 2: podium_order.append(leaderboard[1])
    if n_agents >= 1: podium_order.append(leaderboard[0])
    if n_agents >= 3: podium_order.append(leaderboard[2])
    for entry in podium_order:
        r = entry["rank"]
        cfg = RANK_CONFIG.get(r, {"icon": f"#{r}", "color": "#4f8ef7", "glow": "rgba(79,142,247,0.2)", "label": ""})
        s = entry["stats"]
        height = {1: 120, 2: 90, 3: 70}.get(r, 60)
        initials = agent_initials(entry["agent"])
        is_mock = entry["agent"] in mock_agents
        mock_badge = ' <span class="mock-badge">MOCK</span>' if is_mock else ""
        podium_html += f"""
        <div class="podium-slot rank-{r}" style="--rank-color:{cfg['color']};--rank-glow:{cfg['glow']}">
          <div class="podium-avatar">{initials}</div>
          <div class="podium-agent">{entry['agent']}{mock_badge}</div>
          <div class="podium-score">{s['diag_pct']}%</div>
          <div class="podium-label">{s['total']} problems</div>
          <div class="podium-base" style="height:{height}px">
            <span class="podium-rank-badge">{cfg['icon']}</span>
          </div>
        </div>"""

    # --- Stat cards ---
    top_agent = leaderboard[0]["agent"] if leaderboard else "—"
    top_score = leaderboard[0]["stats"]["diag_pct"] if leaderboard else 0
    total_runs = sum(e["stats"]["total"] for e in leaderboard)

    # --- Rankings rows ---
    table_rows = ""
    for entry in leaderboard:
        rank = entry["rank"]
        agent = entry["agent"]
        s = entry["stats"]
        cfg = RANK_CONFIG.get(rank, {"color": "#4f8ef7", "glow": "rgba(79,142,247,0.1)"})
        rank_badge = f'<span class="rank-num" style="color:{cfg["color"]}">{RANK_CONFIG[rank]["icon"] if rank <= 3 else f"#{rank}"}</span>'
        initials = agent_initials(agent)
        is_mock = agent in mock_agents
        mock_tag = ' <span class="mock-badge">MOCK DATA</span>' if is_mock else ""
        row_class = f"rank-row top{rank}" if rank <= 3 else "rank-row"

        ttl_val = fmt_time(s["avg_ttl"])
        ttm_val = fmt_time(s["avg_ttm"])

        table_rows += f"""
        <tr class="{row_class}" style="--row-glow:{cfg['glow']};--row-color:{cfg['color']}">
          <td class="td-rank">{rank_badge}</td>
          <td class="td-agent">
            <div class="agent-chip">
              <div class="agent-avatar" style="background:linear-gradient(135deg,{cfg['color']}33,{cfg['color']}11);border-color:{cfg['color']}44">{initials}</div>
              <span class="agent-label">{agent}{mock_tag}</span>
            </div>
          </td>
          <td class="td-center"><span class="pill-count">{s['total']}</span></td>
          <td class="td-bar">
            <div class="bar-row">
              <div class="bar-track"><div class="bar-fill diag-fill" style="--w:{s['diag_pct']}%;--color:#4f8ef7" data-pct="{s['diag_pct']}"></div></div>
              <span class="bar-pct">{s['diag_pct']}%</span>
            </div>
          </td>
          <td class="td-bar">
            <div class="bar-row">
              <div class="bar-track"><div class="bar-fill mitig-fill" style="--w:{s['mitig_pct']}%;--color:#34d399" data-pct="{s['mitig_pct']}"></div></div>
              <span class="bar-pct">{s['mitig_pct']}%</span>
            </div>
          </td>
          <td class="td-time"><span class="time-badge">{ttl_val}</span></td>
          <td class="td-time"><span class="time-badge">{ttm_val}</span></td>
        </tr>"""

    # --- Category grid ---
    cat_cards = ""
    for cat in all_categories:
        cfg = CATEGORY_COLORS.get(cat, {"hex": "#9ca3af", "glow": "rgba(156,163,175,0.3)"})
        bars = ""
        for entry in leaderboard:
            cs = entry["stats"]["categories"].get(cat)
            pct = cs["score"] if cs else 0
            bars += f"""
            <div class="cat-bar-row">
              <span class="cat-agent-label">{entry['agent']}</span>
              <div class="cat-bar-track">
                <div class="cat-bar-fill" style="width:{pct}%;background:{cfg['hex']};box-shadow:0 0 8px {cfg['glow']}"></div>
              </div>
              <span class="cat-pct" style="color:{cfg['hex']}">{pct}%</span>
            </div>"""
        cat_cards += f"""
        <div class="cat-card" style="--cat-color:{cfg['hex']};--cat-glow:{cfg['glow']}">
          <div class="cat-card-header">
            <div class="cat-dot" style="background:{cfg['hex']};box-shadow:0 0 10px {cfg['glow']}"></div>
            <span class="cat-name">{cat}</span>
          </div>
          <div class="cat-bars">{bars}</div>
        </div>"""

    # --- Heatmap ---
    sorted_problems = sorted(all_problems, key=lambda p: (get_category(p), p))
    heatmap_header = '<th class="hm-th hm-sticky-col">Agent</th>'
    for p in sorted_problems:
        cat = get_category(p)
        color = CATEGORY_COLORS.get(cat, {"hex": "#9ca3af"})["hex"]
        heatmap_header += f'<th class="hm-th prob-th" title="{p}" style="--cat-color:{color}"><div class="prob-label">{p}</div></th>'

    heatmap_rows = ""
    for entry in leaderboard:
        agent = entry["agent"]
        problems = entry["stats"]["problems"]
        cells = f'<td class="hm-td hm-sticky-col"><span class="hm-agent">{agent}</span></td>'
        for p in sorted_problems:
            result = problems.get(p)
            if result is None:
                cells += f'<td class="hm-td hm-na" title="{p} — not tested"></td>'
            elif result["diag"] and result["mitig"] is not False:
                cells += f'<td class="hm-td hm-pass" title="{p} — PASS"></td>'
            elif result["diag"]:
                cells += f'<td class="hm-td hm-diag" title="{p} — Diagnosis only"></td>'
            else:
                cells += f'<td class="hm-td hm-fail" title="{p} — FAIL"></td>'
        heatmap_rows += f"<tr>{cells}</tr>"

    # --- JS data ---
    chart_json = json.dumps([{
        "agent": e["agent"],
        "diag_pct": e["stats"]["diag_pct"],
        "mitig_pct": e["stats"]["mitig_pct"],
        "total": e["stats"]["total"],
        "categories": {cat: e["stats"]["categories"].get(cat, {}).get("score", 0) for cat in all_categories},
    } for e in leaderboard])
    cat_colors_json = json.dumps({cat: CATEGORY_COLORS.get(cat, {"hex": "#9ca3af"})["hex"] for cat in all_categories})

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SREGym Leaderboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ── Reset & Base ─────────────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg:       #050508;
  --bg2:      #0a0a0f;
  --surface:  #0f0f16;
  --surface2: #14141e;
  --border:   rgba(255,255,255,0.06);
  --border2:  rgba(255,255,255,0.10);
  --text:     #f0f0ff;
  --muted:    #6b7280;
  --muted2:   #9ca3af;
  --accent:   #4f8ef7;
  --accent2:  #818cf8;
  --pass:     #34d399;
  --fail:     #f87171;
  --diag:     #fbbf24;
  --gold:     #FFD700;
  --silver:   #C0C0C0;
  --bronze:   #CD7F32;
  font-size: 14px;
}}
html {{ scroll-behavior: smooth; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', -apple-system, sans-serif;
  min-height: 100vh;
  overflow-x: hidden;
}}

/* ── Scrollbar ────────────────────────────────────────────── */
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: var(--bg2); }}
::-webkit-scrollbar-thumb {{ background: #2a2a3a; border-radius: 3px; }}
::-webkit-scrollbar-thumb:hover {{ background: #3a3a5a; }}

/* ── Hero Header ──────────────────────────────────────────── */
.hero {{
  position: relative;
  overflow: hidden;
  padding: 56px 48px 48px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
}}
.hero-grid {{
  position: absolute; inset: 0; z-index: 0;
  background-image:
    linear-gradient(rgba(79,142,247,0.04) 1px, transparent 1px),
    linear-gradient(90deg, rgba(79,142,247,0.04) 1px, transparent 1px);
  background-size: 40px 40px;
  mask-image: radial-gradient(ellipse 80% 60% at 50% 0%, black 40%, transparent 100%);
}}
.hero-glow {{
  position: absolute; inset: 0; z-index: 0;
  background: radial-gradient(ellipse 60% 40% at 30% 0%, rgba(79,142,247,0.12) 0%, transparent 60%),
              radial-gradient(ellipse 40% 30% at 80% 20%, rgba(129,140,248,0.08) 0%, transparent 50%);
  pointer-events: none;
}}
.hero-content {{ position: relative; z-index: 1; }}
.hero-eyebrow {{
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11px; font-weight: 600; letter-spacing: 2px; text-transform: uppercase;
  color: var(--accent); background: rgba(79,142,247,0.1);
  border: 1px solid rgba(79,142,247,0.2); border-radius: 100px;
  padding: 4px 12px; margin-bottom: 16px;
}}
.hero-eyebrow .dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--accent); animation: pulse 2s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; transform:scale(1); }} 50% {{ opacity:0.5; transform:scale(0.8); }} }}
.hero h1 {{
  font-size: clamp(36px, 5vw, 56px); font-weight: 900; letter-spacing: -2px; line-height: 1;
  background: linear-gradient(135deg, #ffffff 0%, #a5b4fc 60%, #4f8ef7 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  margin-bottom: 10px;
}}
.hero-sub {{ color: var(--muted2); font-size: 15px; font-weight: 400; letter-spacing: 0; }}
.hero-sub strong {{ color: var(--text); font-weight: 600; }}
.hero-stats {{
  display: flex; gap: 32px; margin-top: 32px; flex-wrap: wrap;
}}
.hero-stat {{
  display: flex; flex-direction: column; gap: 2px;
}}
.hero-stat .val {{
  font-size: 28px; font-weight: 800; letter-spacing: -1px;
  background: linear-gradient(135deg, #fff, #a5b4fc);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}}
.hero-stat .lbl {{ font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }}

/* ── Nav Tabs ─────────────────────────────────────────────── */
.nav {{
  display: flex; align-items: center; gap: 2px;
  padding: 0 48px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
  backdrop-filter: blur(20px);
}}
.tab-btn {{
  position: relative;
  padding: 14px 20px;
  font-size: 13px; font-weight: 500;
  color: var(--muted); cursor: pointer;
  border: none; background: none;
  transition: color .2s;
  white-space: nowrap;
}}
.tab-btn::after {{
  content: ''; position: absolute; bottom: 0; left: 20px; right: 20px; height: 2px;
  background: var(--accent); border-radius: 2px 2px 0 0;
  transform: scaleX(0); transition: transform .2s;
}}
.tab-btn:hover {{ color: var(--text); }}
.tab-btn.active {{ color: var(--text); }}
.tab-btn.active::after {{ transform: scaleX(1); }}

/* ── Panels ───────────────────────────────────────────────── */
.panel {{ display: none; padding: 40px 48px; animation: fadeIn .2s ease; }}
.panel.active {{ display: block; }}
@keyframes fadeIn {{ from {{ opacity:0; transform:translateY(4px); }} to {{ opacity:1; transform:translateY(0); }} }}

/* ── Section headers ──────────────────────────────────────── */
.section-header {{
  display: flex; align-items: baseline; gap: 12px; margin-bottom: 24px;
}}
.section-title {{
  font-size: 20px; font-weight: 700; letter-spacing: -0.5px;
}}
.section-sub {{ font-size: 13px; color: var(--muted); }}

/* ── Podium ───────────────────────────────────────────────── */
.podium-wrap {{
  display: flex; align-items: flex-end; justify-content: center;
  gap: 12px; margin-bottom: 40px; padding: 0 0 0;
}}
.podium-slot {{
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  flex: 0 0 180px;
}}
.podium-avatar {{
  width: 56px; height: 56px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; font-weight: 800; letter-spacing: -1px;
  background: linear-gradient(135deg, var(--rank-color)22, var(--rank-color)11);
  border: 2px solid var(--rank-color);
  box-shadow: 0 0 20px var(--rank-glow), 0 0 40px var(--rank-glow);
  color: var(--rank-color);
  transition: transform .3s;
}}
.podium-slot:hover .podium-avatar {{ transform: translateY(-4px) scale(1.05); }}
.podium-agent {{ font-size: 14px; font-weight: 700; color: var(--text); }}
.podium-score {{
  font-size: 22px; font-weight: 900; letter-spacing: -1px;
  color: var(--rank-color);
  text-shadow: 0 0 20px var(--rank-glow);
}}
.podium-label {{ font-size: 11px; color: var(--muted); }}
.podium-base {{
  width: 100%;
  background: linear-gradient(180deg, var(--rank-color)18 0%, var(--rank-color)08 100%);
  border: 1px solid var(--rank-color)33;
  border-radius: 8px 8px 0 0;
  display: flex; align-items: flex-start; justify-content: center;
  padding-top: 10px;
  box-shadow: 0 -4px 20px var(--rank-glow);
}}
.podium-rank-badge {{
  font-size: 11px; font-weight: 800; letter-spacing: 2px;
  color: var(--rank-color); opacity: 0.8;
}}

/* ── Leaderboard Table ────────────────────────────────────── */
.lb-table-wrap {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; overflow: hidden; margin-bottom: 32px;
}}
.lb-table {{ width: 100%; border-collapse: collapse; }}
.lb-table thead tr {{
  background: var(--surface2);
  border-bottom: 1px solid var(--border2);
}}
.lb-table th {{
  padding: 12px 16px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px; color: var(--muted);
  text-align: left; white-space: nowrap;
}}
.lb-table td {{ padding: 0; border-bottom: 1px solid var(--border); vertical-align: middle; }}
.lb-table tbody tr:last-child td {{ border-bottom: none; }}
.lb-table tbody tr.rank-row {{
  position: relative; transition: background .15s;
}}
.lb-table tbody tr.rank-row:hover {{ background: rgba(255,255,255,0.02); }}
.lb-table tbody tr.top1 {{ background: rgba(255,215,0,0.03); }}
.lb-table tbody tr.top1:hover {{ background: rgba(255,215,0,0.05); }}
.lb-table tbody tr.top2 {{ background: rgba(192,192,192,0.02); }}
.lb-table tbody tr.top3 {{ background: rgba(205,127,50,0.02); }}

.td-rank {{ padding: 16px 8px 16px 20px; width: 60px; text-align: center; }}
.rank-num {{ font-size: 12px; font-weight: 800; letter-spacing: 1px; font-family: 'JetBrains Mono', monospace; }}
.td-agent {{ padding: 12px 16px; }}
.agent-chip {{ display: flex; align-items: center; gap: 10px; }}
.agent-avatar {{
  width: 36px; height: 36px; border-radius: 10px; flex-shrink: 0;
  border: 1px solid transparent;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 800; color: var(--text);
}}
.agent-label {{ font-size: 14px; font-weight: 600; letter-spacing: -0.3px; }}
.td-center {{ padding: 12px 16px; text-align: center; }}
.pill-count {{
  display: inline-block; padding: 2px 10px; border-radius: 100px;
  font-size: 12px; font-weight: 600; font-family: 'JetBrains Mono', monospace;
  background: rgba(255,255,255,0.06); color: var(--muted2);
}}
.td-bar {{ padding: 12px 16px; min-width: 200px; }}
.bar-row {{ display: flex; align-items: center; gap: 10px; }}
.bar-track {{
  flex: 1; height: 6px; background: rgba(255,255,255,0.05);
  border-radius: 100px; overflow: hidden;
}}
.bar-fill {{
  height: 100%; border-radius: 100px; width: 0;
  transition: width 1s cubic-bezier(0.16,1,0.3,1);
  box-shadow: 0 0 8px var(--color);
  background: var(--color);
}}
.bar-pct {{ font-size: 12px; font-weight: 600; color: var(--muted2); min-width: 38px; font-family: 'JetBrains Mono', monospace; }}
.td-time {{ padding: 12px 16px; }}
.time-badge {{
  font-size: 12px; font-family: 'JetBrains Mono', monospace;
  color: var(--muted2); background: rgba(255,255,255,0.04);
  padding: 3px 8px; border-radius: 6px;
}}

/* ── Category Grid ────────────────────────────────────────── */
.cat-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px; margin-bottom: 32px;
}}
.cat-card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 14px; padding: 20px; transition: border-color .2s, box-shadow .2s;
}}
.cat-card:hover {{
  border-color: var(--cat-color)44;
  box-shadow: 0 0 20px var(--cat-glow);
}}
.cat-card-header {{
  display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
}}
.cat-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
.cat-name {{ font-size: 13px; font-weight: 700; letter-spacing: -0.2px; }}
.cat-bars {{ display: flex; flex-direction: column; gap: 8px; }}
.cat-bar-row {{ display: flex; align-items: center; gap: 8px; }}
.cat-agent-label {{ font-size: 11px; color: var(--muted); width: 80px; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.cat-bar-track {{
  flex: 1; height: 5px; background: rgba(255,255,255,0.05);
  border-radius: 100px; overflow: hidden;
}}
.cat-bar-fill {{
  height: 100%; border-radius: 100px; width: 0;
  transition: width 1.2s cubic-bezier(0.16,1,0.3,1);
}}
.cat-pct {{ font-size: 11px; font-weight: 700; font-family: 'JetBrains Mono', monospace; min-width: 32px; text-align: right; }}

/* ── Heatmap ──────────────────────────────────────────────── */
.heatmap-outer {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; overflow: hidden;
}}
.heatmap-scroll {{ overflow-x: auto; }}
.hm-table {{ border-collapse: collapse; }}
.hm-th {{
  padding: 8px 4px; font-size: 10px; font-weight: 500; color: var(--muted);
  border-bottom: 1px solid var(--border); white-space: nowrap;
  background: var(--surface2); text-align: center;
}}
.prob-th {{ height: 120px; vertical-align: bottom; padding-bottom: 6px; }}
.prob-label {{
  writing-mode: vertical-rl; text-orientation: mixed;
  transform: rotate(180deg);
  font-size: 10px; color: var(--muted); max-height: 110px;
  overflow: hidden; text-overflow: ellipsis;
  padding: 0 2px;
  border-left: 2px solid var(--cat-color);
}}
.hm-sticky-col {{
  position: sticky; left: 0; z-index: 2;
  background: var(--surface2); border-right: 1px solid var(--border2);
  min-width: 110px;
}}
.hm-td {{
  width: 18px; min-width: 18px; height: 18px;
  border: 1px solid rgba(255,255,255,0.03);
  cursor: default; transition: transform .1s, opacity .1s;
}}
.hm-td:hover {{ transform: scale(1.5); z-index: 10; position: relative; opacity: 1 !important; }}
.hm-pass {{ background: rgba(52,211,153,0.5); }}
.hm-fail {{ background: rgba(248,113,113,0.25); }}
.hm-diag {{ background: rgba(251,191,36,0.35); }}
.hm-na {{ background: rgba(255,255,255,0.02); }}
.hm-agent {{ font-size: 12px; font-weight: 600; padding: 8px 12px; display: block; color: var(--text); }}

/* ── Legend ───────────────────────────────────────────────── */
.legend-row {{
  display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap;
}}
.legend-item {{
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--muted2); font-weight: 500;
}}
.leg-swatch {{ width: 14px; height: 14px; border-radius: 3px; }}
.mock-badge {{
  display: inline-block; font-size: 9px; font-weight: 700; letter-spacing: 1px;
  text-transform: uppercase; vertical-align: middle;
  padding: 2px 6px; border-radius: 4px; margin-left: 6px;
  background: rgba(251,191,36,0.12); color: #fbbf24;
  border: 1px solid rgba(251,191,36,0.3);
}}
</style>
</head>
<body>

<!-- HERO -->
<header class="hero">
  <div class="hero-grid"></div>
  <div class="hero-glow"></div>
  <div class="hero-content">
    <div class="hero-eyebrow"><span class="dot"></span>Live Rankings</div>
    <h1>SREGym Leaderboard</h1>
    <p class="hero-sub">Competitive benchmark for AI agents solving real-world SRE incidents</p>
    <div class="hero-stats">
      <div class="hero-stat">
        <span class="val" id="stat-agents">{n_agents}</span>
        <span class="lbl">Agents Competing</span>
      </div>
      <div class="hero-stat">
        <span class="val" id="stat-problems">{n_problems}</span>
        <span class="lbl">Problems</span>
      </div>
      <div class="hero-stat">
        <span class="val" id="stat-runs">{total_runs}</span>
        <span class="lbl">Total Runs</span>
      </div>
      <div class="hero-stat">
        <span class="val" id="stat-leader">{top_score}%</span>
        <span class="lbl">Top Score ({top_agent})</span>
      </div>
    </div>
  </div>
</header>

<!-- NAV -->
<nav class="nav">
  <button class="tab-btn active" onclick="showTab('rankings',this)">Rankings</button>
  <button class="tab-btn" onclick="showTab('categories',this)">By Category</button>
  <button class="tab-btn" onclick="showTab('heatmap',this)">Problem Heatmap</button>
</nav>

<!-- RANKINGS -->
<div id="panel-rankings" class="panel active">
  <div class="podium-wrap">{podium_html}</div>

  <div class="section-header">
    <span class="section-title">All Agents</span>
    <span class="section-sub">Ranked by diagnosis pass rate · best attempt per problem</span>
  </div>
  <div class="lb-table-wrap">
    <table class="lb-table">
      <thead>
        <tr>
          <th></th><th>Agent</th>
          <th style="text-align:center">Problems</th>
          <th>Diagnosis %</th>
          <th>Mitigation %</th>
          <th>Avg Time · Diag</th>
          <th>Avg Time · Mitig</th>
        </tr>
      </thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
</div>

<!-- CATEGORIES -->
<div id="panel-categories" class="panel">
  <div class="section-header">
    <span class="section-title">Performance by Fault Category</span>
    <span class="section-sub">Diagnosis pass rate per agent per fault type</span>
  </div>
  <div class="cat-grid">{cat_cards}</div>
</div>

<!-- HEATMAP -->
<div id="panel-heatmap" class="panel">
  <div class="section-header">
    <span class="section-title">Problem Heatmap</span>
    <span class="section-sub">Every problem × every agent</span>
  </div>
  <div class="legend-row">
    <div class="legend-item"><div class="leg-swatch" style="background:rgba(52,211,153,0.5)"></div>Full Pass</div>
    <div class="legend-item"><div class="leg-swatch" style="background:rgba(251,191,36,0.35)"></div>Diagnosis Only</div>
    <div class="legend-item"><div class="leg-swatch" style="background:rgba(248,113,113,0.25)"></div>Fail</div>
    <div class="legend-item"><div class="leg-swatch" style="background:rgba(255,255,255,0.04)"></div>Not Tested</div>
  </div>
  <div class="heatmap-outer">
    <div class="heatmap-scroll">
      <table class="hm-table">
        <thead><tr>{heatmap_header}</tr></thead>
        <tbody>{heatmap_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<script>
// ── Tab switching ────────────────────────────────────────────
function showTab(name, el) {{
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(t => t.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  el.classList.add('active');
  if (name === 'rankings') animateBars();
  if (name === 'categories') animateCatBars();
}}

// ── Animate bars on load ─────────────────────────────────────
function animateBars() {{
  document.querySelectorAll('.bar-fill').forEach(el => {{
    el.style.width = '0%';
    const target = el.dataset.pct + '%';
    requestAnimationFrame(() => {{
      requestAnimationFrame(() => {{ el.style.width = target; }});
    }});
  }});
}}

function animateCatBars() {{
  document.querySelectorAll('.cat-bar-fill').forEach(el => {{
    const w = el.style.width;
    el.style.width = '0%';
    requestAnimationFrame(() => {{ requestAnimationFrame(() => {{ el.style.width = w; }}); }});
  }});
}}

// ── Counter animation ────────────────────────────────────────
function animateCounter(el, target, suffix) {{
  const duration = 1200;
  const start = performance.now();
  const isFloat = String(target).includes('.');
  function step(now) {{
    const t = Math.min((now - start) / duration, 1);
    const ease = 1 - Math.pow(1 - t, 3);
    const val = isFloat ? (target * ease).toFixed(1) : Math.round(target * ease);
    el.textContent = val + suffix;
    if (t < 1) requestAnimationFrame(step);
  }}
  requestAnimationFrame(step);
}}

// Run on load
window.addEventListener('load', () => {{
  animateBars();
  const agentEl = document.getElementById('stat-agents');
  const probEl  = document.getElementById('stat-problems');
  const runsEl  = document.getElementById('stat-runs');
  const leadEl  = document.getElementById('stat-leader');
  if (agentEl) animateCounter(agentEl, {n_agents}, '');
  if (probEl)  animateCounter(probEl,  {n_problems}, '');
  if (runsEl)  animateCounter(runsEl,  {total_runs}, '');
  if (leadEl)  animateCounter(leadEl,  {top_score}, '%');
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Terminal preview (ANSI)
# ---------------------------------------------------------------------------
# ANSI helpers
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def _bold(t):   return _c("1", t)
def _dim(t):    return _c("2", t)
def _gold(t):   return _c("1;33", t)
def _silver(t): return _c("1;37", t)
def _bronze(t): return _c("38;5;130", t)
def _blue(t):   return _c("1;34", t)
def _green(t):  return _c("32", t)
def _red(t):    return _c("31", t)
def _yellow(t): return _c("33", t)
def _cyan(t):   return _c("36", t)
def _magenta(t):return _c("35", t)
def _grey(t):   return _c("90", t)
def _bg_blue(t):return _c("44;1;97", t)

RANK_FMT = {1: _gold, 2: _silver, 3: _bronze}
CAT_FMT = {
    "K8s Config":        _blue,
    "Application Faults":_yellow,
    "K8s Operator":      _magenta,
    "OpenTelemetry":     _green,
    "Metastable":        _red,
    "Correlated Faults": _yellow,
    "Hardware Faults":   _cyan,
    "Train Ticket":      _magenta,
    "Other":             _grey,
}

def _pct_bar(pct: float, width: int = 20, fill_char: str = "█", empty_char: str = "░") -> str:
    filled = round(pct / 100 * width)
    return fill_char * filled + _grey(empty_char * (width - filled))

def _rank_icon(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank} ")

def _fmt_time_term(seconds: int | None) -> str:
    if seconds is None:
        return _grey("  —   ")
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"

def print_preview(preview_agent: str, preview_rows: list[dict], all_agent_rows: dict[str, list[dict]], mock_agents: set[str] = None):
    mock_agents = mock_agents or set()
    """Print a private terminal comparison of preview_agent vs the leaderboard."""

    # Build leaderboard with the preview agent injected (tagged as preview)
    combined = {**all_agent_rows, preview_agent: preview_rows}
    leaderboard = build_leaderboard(combined)
    all_problems = {r["problem_id"] for rows in combined.values() for r in rows}
    all_categories = sorted(set(get_category(p) for p in all_problems))

    # Find the preview entry
    preview_entry = next(e for e in leaderboard if e["agent"] == preview_agent)
    preview_rank = preview_entry["rank"]
    preview_stats = preview_entry["stats"]
    n_agents = len(leaderboard)
    W = 80  # terminal width

    # ── Header ──────────────────────────────────────────────────────────────
    print()
    print(_bold("━" * W))
    print(_bg_blue(f"  SREGym Private Preview  ·  {preview_agent}  ".center(W)))
    print(_bold("━" * W))
    print()

    # ── Your rank card ───────────────────────────────────────────────────────
    rank_color = RANK_FMT.get(preview_rank, _blue)
    rank_icon = _rank_icon(preview_rank)
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(preview_rank, "th")
    print(f"  {rank_icon}  {rank_color(_bold(f'{preview_rank}{suffix} place'))}  out of {_bold(str(n_agents))} agents")
    print()
    diag_pct_str = rank_color(_bold(str(preview_stats['diag_pct']) + "%"))
    mitig_pct_str = _green(_bold(str(preview_stats['mitig_pct']) + "%"))
    print(f"  {'Problems tested:':<22} {_bold(str(preview_stats['total']))}")
    print(f"  {'Diagnosis pass rate:':<22} {diag_pct_str}  {_pct_bar(preview_stats['diag_pct'])}")
    print(f"  {'Mitigation pass rate:':<22} {mitig_pct_str}  {_pct_bar(preview_stats['mitig_pct'])}")
    print(f"  {'Avg time to diagnose:':<22} {_fmt_time_term(preview_stats['avg_ttl'])}")
    print(f"  {'Avg time to mitigate:':<22} {_fmt_time_term(preview_stats['avg_ttm'])}")
    print()
    print(_grey("─" * W))

    # ── Full rankings table ──────────────────────────────────────────────────
    print()
    COL = [6, 18, 9, 26, 26, 10, 10]
    header = (
        f"{'Rank':<{COL[0]}}{'Agent':<{COL[1]}}{'Probs':>{COL[2]}}"
        f"  {'Diagnosis %':<{COL[3]}}{'Mitigation %':<{COL[4]}}"
        f"{'TTD':>{COL[5]}}{'TTM':>{COL[6]}}"
    )
    print("  " + _bold(_grey(header)))
    print("  " + _grey("─" * (W - 2)))

    for entry in leaderboard:
        rank = entry["rank"]
        agent = entry["agent"]
        s = entry["stats"]
        is_me = agent == preview_agent

        rank_str = _rank_icon(rank) if rank <= 3 else _grey(f"  #{rank:<3}")
        rfmt = RANK_FMT.get(rank, lambda x: x)

        mock_tag = _yellow(" [MOCK]") if agent in mock_agents and not is_me else ""
        agent_str = _bg_blue(f" {agent} ") if is_me else (rfmt(_bold(agent)) if rank <= 3 else agent)
        agent_str = agent_str + mock_tag
        probs_str = str(s["total"])
        d_bar = _pct_bar(s["diag_pct"], 12) + f" {s['diag_pct']:>5.1f}%"
        m_bar = _pct_bar(s["mitig_pct"], 12) + f" {s['mitig_pct']:>5.1f}%"
        ttd = _fmt_time_term(s["avg_ttl"])
        ttm = _fmt_time_term(s["avg_ttm"])

        row = f"{rank_str:<6}  {agent_str:<18}  {probs_str:>5}  {d_bar}  {m_bar}  {ttd}  {ttm}"
        if is_me:
            print("  " + _bold(row))
        else:
            print("  " + row)

    print()
    print(_grey("─" * W))

    # ── Category breakdown ───────────────────────────────────────────────────
    print()
    print("  " + _bold("Category Breakdown"))
    print()
    col_w = 22
    cat_header = f"  {'Category':<{col_w}}" + "".join(f"{e['agent'][:10]:>12}" for e in leaderboard)
    print(_bold(_grey(cat_header)))
    print(_grey("  " + "─" * min(W, len(cat_header))))

    for cat in all_categories:
        cfmt = CAT_FMT.get(cat, _grey)
        row = f"  {cfmt(cat[:col_w-1]):<{col_w + 9}}"  # +9 for ANSI codes
        for entry in leaderboard:
            cs = entry["stats"]["categories"].get(cat)
            pct = cs["score"] if cs else None
            agent = entry["agent"]
            is_me = agent == preview_agent
            if pct is None:
                cell = _grey("      —  ")
            else:
                cell_str = f"{pct:>4}%"
                cell = _bold(_bg_blue(f" {cell_str} ")) if is_me else (
                    _green(cell_str) if pct >= 70 else
                    _yellow(cell_str) if pct >= 40 else
                    _red(cell_str)
                )
            row += f"  {cell:>10}"
        print(row)

    print()
    print(_grey("─" * W))

    # ── Per-problem breakdown for the preview agent ──────────────────────────
    print()
    print("  " + _bold(f"Your results — {preview_agent}"))
    print()
    problems_by_cat: dict[str, list] = defaultdict(list)
    for pid, result in sorted(preview_stats["problems"].items()):
        problems_by_cat[get_category(pid)].append((pid, result))

    for cat, items in sorted(problems_by_cat.items()):
        cfmt = CAT_FMT.get(cat, _grey)
        print(f"  {cfmt(_bold(cat))}")
        for pid, result in items:
            d = result["diag"]
            m = result["mitig"]
            if d and m is not False:
                status = _green("✓ PASS     ")
            elif d:
                status = _yellow("~ DIAG ONLY")
            else:
                status = _red("✗ FAIL     ")
            print(f"    {status}  {_grey(pid)}")
        print()

    # ── Footer ───────────────────────────────────────────────────────────────
    print(_bold("━" * W))
    print(_grey(f"  Results are private — not submitted to the public leaderboard."))
    print(_grey(f"  To submit: copy your CSV to leaderboard/submissions/{preview_agent}/ and regenerate."))
    print(_bold("━" * W))
    print()


def load_preview_csv(path: Path, agent_name: str) -> list[dict]:
    """Load a CSV or directory of CSVs as preview rows for agent_name."""
    rows = []
    paths = list(path.glob("*.csv")) if path.is_dir() else [path]
    for p in paths:
        for row in load_csv(p):
            pid = row.get("problem_id", "").strip()
            if not pid:
                continue
            rows.append({
                "problem_id": pid,
                "diag_success": parse_bool(row.get("Diagnosis.success")),
                "mitig_success": parse_bool(row.get("Mitigation.success")),
                "ttl": _float(row.get("TTL")),
                "ttm": _float(row.get("TTM")),
                "agent": agent_name,
            })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="SREGym Leaderboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  (default)   Generate leaderboard.html
  preview     Show private terminal comparison before submitting

examples:
  python leaderboard/leaderboard.py
  python leaderboard/leaderboard.py preview --agent ciroos --csv results.csv
  python leaderboard/leaderboard.py preview --agent ciroos --csv ./my_results/
""")
    parser.add_argument("--root", default=None, help="Path to SREGym root")
    parser.add_argument("--output", default="leaderboard/leaderboard.html")

    subparsers = parser.add_subparsers(dest="command")
    preview_parser = subparsers.add_parser("preview", help="Private terminal preview of your results")
    preview_parser.add_argument("--agent", required=True, help="Your agent name (display label)")
    preview_parser.add_argument("--csv", required=True, help="Path to your results CSV or directory of CSVs")

    args = parser.parse_args()
    root = Path(args.root) if args.root else Path(__file__).parent.parent

    if args.command == "preview":
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"Error: {csv_path} does not exist.", file=sys.stderr)
            sys.exit(1)

        preview_rows = load_preview_csv(csv_path, args.agent)
        if not preview_rows:
            print("Error: no valid rows found in the CSV (need at least problem_id + Diagnosis.success).", file=sys.stderr)
            sys.exit(1)

        # Load existing public leaderboard data (exclude the preview agent if somehow present)
        all_agent_rows = load_all_results(root)
        all_agent_rows.pop(args.agent, None)  # don't double-count if name collides

        print_preview(args.agent, preview_rows, all_agent_rows, load_mock_agents(root))
        return

    # Default: generate HTML
    print(f"Loading results from {root}...")
    agent_rows = load_all_results(root)

    if not agent_rows:
        print("No results found. Make sure *_ALL_results.csv files exist in the root directory.")
        return

    for agent, rows in agent_rows.items():
        print(f"  {agent}: {len(rows)} problem runs")

    leaderboard = build_leaderboard(agent_rows)
    all_problems = {row["problem_id"] for rows in agent_rows.values() for row in rows}
    mock_agents = load_mock_agents(root)
    if mock_agents:
        print(f"  Mock agents: {', '.join(sorted(mock_agents))}")

    html = render_html(leaderboard, all_problems, mock_agents)
    out_path = root / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"\nLeaderboard saved to: {out_path}")
    print(f"Open in browser:  open {out_path}")


if __name__ == "__main__":
    main()
