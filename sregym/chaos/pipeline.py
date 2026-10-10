#!/usr/bin/env python3
"""Chaos-agent problem generation pipeline (host-side orchestrator).

Every phase runs in its own DinD environment (private Docker daemon + kind
cluster, see docker/dind/README.md) through the normal ``main.py`` flow:

  1. chaos    a codex/claudecode agent breaks a healthy deployment of the
              ``chaos_canvas_<app>`` problem and writes a fault artifact.
  2. validate the artifact replays as ``gen_<id>`` on fresh deployments with
              the scripted ``refmit`` agent; gates: impact, observability,
              persistence, integrity, reproducibility, solvability, no leaks.
  3. panel    fixer agents get the standard 30-minute budget, k attempts
              each, in two rounds; the problem is accepted when the solve rate
              stays within the target band (default: at most 1 of 4).

Accepted artifacts are copied into sregym/conductor/problems/generated/, where
the registry picks them up as ``gen_<id>``. State lives in <work>/ledger.json,
so the pipeline can be stopped and resumed. Run with access to the Docker
daemon (e.g. sudo), from the repository root:

  sudo HOME=$HOME python3 sregym/chaos/pipeline.py --work /mnt/docker/chaos --slots 3
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sregym.chaos.artifact import GENERATED_ROOT, FaultArtifact  # noqa: E402

AUTH = Path.home() / ".codex" / "auth.json"
LOCK = threading.RLock()

THEMES = [
    ("data", "State in a datastore (MongoDB, Memcached, Redis) or seeded application data makes requests "
             "fail or slow down, while every pod stays Running and Ready."),
    ("discovery", "Service discovery or routing: requests reach the wrong backend, a stale endpoint, or "
                  "only a subset of instances misbehave."),
    ("capacity", "Resource, concurrency or QoS settings (limits, throttling, connection pools, worker counts) "
                 "that only break under the existing production load, causing tail latency or errors."),
    ("app-config", "A plausible application configuration change (ConfigMap, env, args, config files) whose "
                   "semantics subtly break request handling."),
    ("network", "The network path between specific components (policies, ports, protocols, timeouts, MTU-like "
                "behaviour) is partially broken; most traffic still flows."),
    ("multi-cause", "One coherent incident with two contributing changes; fixing only the most visible one "
                    "leaves users broken."),
    ("controller", "Kubernetes controllers or companion objects (HPA, PDB, CronJobs, init containers, jobs, "
                   "owners, admission-free mechanisms) perpetuate or re-apply the bad state so naive fixes "
                   "do not stick."),
    ("code", "Application logic deployed through the cluster (scripts, Lua/nginx config, templated config, "
             "command lines) contains a realistic bug, like a bad code change."),
    ("identity", "Credential, identity or certificate rotation left components inconsistent with each other."),
    ("metastable", "Retry, timeout or caching behaviour creates a self-sustaining degraded state."),
]
APPS = ["hotel_reservation", "social_network"]


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fixer_digest(out: Path, limit: int = 4000) -> str:
    """Commands and messages of a fixer run, compact enough to brief a hardening session."""
    lines = []
    for path in sorted(out.rglob("codex.txt")):
        for raw in path.read_text(errors="ignore").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if event.get("type") != "item.completed":
                continue
            if item.get("type") == "agent_message":
                lines.append("SAYS: " + (item.get("text") or "")[:400].replace("\n", " "))
            elif item.get("type") == "command_execution":
                lines.append("RUNS: " + item.get("command", "")[:300].replace("\n", " "))
    for path in sorted(out.rglob("*.jsonl")):  # Claude Code stream-json
        if "claude" not in path.name and "stream" not in path.name:
            continue
        for raw in path.read_text(errors="ignore").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    lines.append("SAYS: " + block.get("text", "")[:400].replace("\n", " "))
                elif block.get("type") == "tool_use":
                    lines.append("RUNS: " + json.dumps(block.get("input"))[:300])
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit // 3] + "\n[...]\n" + text[-2 * limit // 3 :]
    return text


@dataclass
class Job:
    kind: str  # chaos | validate | panel
    cid: str
    priority: int
    label: str
    args: dict = field(default_factory=dict)


class Pipeline:
    def __init__(self, opts):
        self.o = opts
        self.work: Path = opts.work
        self.ledger_path = self.work / "ledger.json"
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {
            "candidates": {},
            "sessions": 0,
        }
        self.running: dict[str, Job] = {}
        self.log_file = open(self.work / "pipeline.log", "a")

    # ------------------------------------------------------------- utilities
    def log(self, msg: str) -> None:
        with LOCK:
            line = f"[{now()}] {msg}"
            print(line, flush=True)
            self.log_file.write(line + "\n")
            self.log_file.flush()

    def save(self) -> None:
        with LOCK:
            tmp = self.ledger_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.ledger, indent=2))
            tmp.replace(self.ledger_path)

    def cand(self, cid: str) -> dict:
        return self.ledger["candidates"][cid]

    def dind(self, name: str, out: Path, command: list[str], env: dict, mounts: list[tuple[Path, str]],
             timeout_s: int, claude: bool = False) -> int:
        marker = out / "EXIT"
        running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True,
                                 text=True).stdout.strip() == "true"
        if not running:
            if marker.exists():
                return int(marker.read_text().strip() or 1)
            if self.csv_rows(out) if out.exists() else False:
                return 0  # finished while no pipeline process was watching it
            shutil.rmtree(out, ignore_errors=True)  # stale or partial output
        out.mkdir(parents=True, exist_ok=True)
        auth = out / "codex-auth.json"
        if not auth.exists():
            shutil.copyfile(AUTH, auth)
            auth.chmod(0o600)
        args = [
            "docker", "run", "--rm", "--privileged", "--cgroupns=private", "--name", name,
            "--stop-timeout", "45", "--cpus", str(self.o.cpus), "--memory", self.o.memory,
            "--mount", f"type=bind,src={out},dst=/opt/sregym/results",
            "--mount", f"type=bind,src={auth},dst=/root/.codex/auth.json",
            "--env", "SREGYM_CHAOS_REPORT_DIR=/opt/sregym/results/chaos_gates",
        ]
        if self.o.registry_mirrors:
            args += ["--env", f"SREGYM_REGISTRY_MIRRORS={self.o.registry_mirrors}"]
        if claude and self.o.claude_env_file:
            args += ["--env-file", str(self.o.claude_env_file)]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        for src, dst in mounts:
            args += ["--mount", f"type=bind,src={src},dst={dst},readonly"]
        args += [self.o.image, *command]
        if running:
            # A previous pipeline process started this job; wait for it instead of restarting it.
            self.log(f"ADOPT running container {name}")
            proc = subprocess.Popen(["docker", "wait", name], stdout=subprocess.PIPE, text=True)
            try:
                stdout, _ = proc.communicate(timeout=timeout_s)
                code = int(stdout.strip() or 1)
            except (subprocess.TimeoutExpired, ValueError):
                subprocess.run(["docker", "stop", "-t", "60", name], capture_output=True)
                code = 1
            auth.unlink(missing_ok=True)
            marker.write_text(str(code))
            return code
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
        with open(out / "dind.log", "a") as handle:
            proc = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT)
            try:
                code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "stop", "-t", "60", name], capture_output=True)
                code = proc.wait()
        auth.unlink(missing_ok=True)
        marker.write_text(str(code))
        return code

    def infra_retry(self, c: dict, out: Path, status: str) -> bool:
        """Re-queue a phase whose environment failed before producing results."""
        c["infra_retries"] = c.get("infra_retries", 0) + 1
        shutil.rmtree(out, ignore_errors=True)
        if c["infra_retries"] > self.o.infra_retries:
            return False
        c["status"] = status
        self.log(f"INFRA RETRY {status} ({c['infra_retries']}/{self.o.infra_retries})")
        return True

    @staticmethod
    def csv_rows(out: Path) -> list[dict]:
        rows = []
        for path in sorted(out.rglob("*_results.csv")):
            if path.name.startswith("_running_") or "_ALL_" in path.name:
                continue
            with open(path) as f:
                rows += list(csv.DictReader(f))
        return rows

    @staticmethod
    def gate_reports(out: Path, kind: str) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted((out / "chaos_gates").glob(f"*.{kind}.*.json"))] \
            if (out / "chaos_gates").is_dir() else []

    def main_cmd(self, problem: str, agent: str, model: str, attempts: int, timeout_s: int,
                 extra: list[str] | None = None) -> list[str]:
        return [
            "uv", "run", "--frozen", "main.py", "--problem", problem, "--agent", agent, "--model", model,
            "--stages", "mitigation", "--n-attempts", str(attempts), "--agent-timeout", str(timeout_s),
            "--judge-backend", "codex", *(extra or []),
        ]

    # ----------------------------------------------------------------- brief
    def avoid_list(self) -> str:
        sys.path.insert(0, str(REPO))
        lines = []
        try:
            registry = (REPO / "sregym/conductor/problems/registry.py").read_text()
            import re

            ids = sorted(set(re.findall(r'^\s+"([a-z0-9_\-]+)":', registry, re.M)))
            ids = [i for i in ids if not i.startswith("chaos_canvas")]
            lines.append("Existing SREGym problem ids (do not recreate these fault types): " + ", ".join(ids))
        except OSError:
            pass
        previous = []
        for c in self.ledger["candidates"].values():
            if not c.get("meta"):
                continue
            line = f"- {c['meta'].get('root_cause_class')}: {c['meta'].get('title')}"
            solved = [a for a in (c.get("panel") or {}).get("attempts", []) if a.get("solved")]
            if solved:
                line += f" -- TOO EASY: a fixer solved it in {float(solved[0].get('ttm') or 0) / 60:.0f} min"
            previous.append(line)
        if previous:
            lines.append("Faults already generated in earlier sessions (choose a different root cause):")
            lines += previous
        return "\n".join(lines)

    def hardening_brief(self, parent_id: str) -> str:
        parent = self.cand(parent_id)
        files = []
        for name in ("fault.json", "design.md", "inject.sh", "mitigate.sh", "recover.sh"):
            path = self.work / "candidates" / parent_id / name
            if path.is_file():
                files.append(f"--- {name} ---\n{path.read_text(errors='ignore')[:6000]}")
        winners = [a for a in parent.get("panel", {}).get("attempts", []) if a.get("solved")]
        for a in winners:
            if not a.get("digest"):
                a["digest"] = fixer_digest(self.work / "runs" / parent_id / f"panel-{a['agent']}-r{a['round']}")
        digests = "\n\n".join(
            f"--- {a['agent']} solved it in {float(a.get('ttm') or 0) / 60:.0f} min ---\n{a.get('digest', '')}"
            for a in winners[:2]
        )
        return (
            "HARDENING SESSION. A previous chaos session produced the incident below. It was valid, but fixer "
            "agents solved it quickly. Their trajectories are included. Evolve it into a harder incident of the "
            "same realistic family: study exactly which evidence led the fixer to the cause and which fix "
            "worked, then change the incident so that path no longer succeeds. For example, the evidence the "
            "fixer followed now plausibly implicates something else; the fix it applied is necessary but not "
            "sufficient (a second independent contributing cause, or something that re-creates the bad "
            "state); or the decisive evidence requires correlating several sources. Every validity rule "
            "still applies. Write a complete new artifact; you may start by applying the parent's inject.sh, "
            "but its scripts are not available in your session, so recreate anything you need.\n\n"
            + "\n\n".join(files) + "\n\n" + digests
        )

    def write_brief(self, cid: str, app: str, theme: tuple[str, str]) -> Path:
        brief = self.work / "candidates-brief" / f"{cid}.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        c = self.cand(cid)
        text = f"Target application: {app}. Set \"app\": \"{app}\" in fault.json.\n"
        if c.get("parent"):
            text += self.hardening_brief(c["parent"]) + "\n\n"
        else:
            text += (
                f"Theme for this session ({theme[0]}): {theme[1]}\n"
                "The theme is a starting direction; pick whatever concrete incident within it you expect to be "
                "hardest for agents while satisfying every validity rule.\n\n"
            )
        text += (
            "Lesson from earlier sessions: fixers start from the logs of the failing services and follow the "
            "first error message that names a dependency; single-hop faults whose logs name the culprit are "
            "solved in minutes. They also dump every Deployment, ConfigMap and database collection early on and "
            "go straight for whatever is not part of the stock application: added sidecars, jobs, collections, "
            "views, recipes or config keys were found within ten minutes however deep the machinery behind them "
            "was. Prefer small edits to values the application already has, so that nothing new stands out "
            "and the obvious repairs (restart, rollback, scale up, cache flush) fail or do not last.\n\n"
            + self.avoid_list() + "\n"
        )
        brief.write_text(text)
        return brief

    # ----------------------------------------------------------------- phases
    def run_chaos(self, job: Job) -> None:
        c = self.cand(job.cid)
        out = self.work / "runs" / job.cid / "chaos"
        brief = self.write_brief(job.cid, c["app"], tuple(c["theme"]))
        backend = c["chaos_backend"]
        model = self.o.model if backend == "codex" else self.o.claude_model
        started = time.time()
        code = self.dind(
            f"chaos-{job.cid}", out,
            self.main_cmd(f"chaos_canvas_{c['app']}", backend, model, 1, self.o.chaos_timeout, ["--force-build"]),
            {"CHAOS_BRIEF_FILE": "/opt/sregym/chaos-brief.md"},
            [(brief, "/opt/sregym/chaos-brief.md")],
            self.o.chaos_timeout + 3600, claude=backend == "claudecode",
        )
        c["chaos"] = {"exit": code, "minutes": round((time.time() - started) / 60, 1)}
        rows = self.csv_rows(out)
        if not rows and self.infra_retry(c, out, "chaos"):
            return
        c["chaos"]["slo_violated_at_submit"] = rows[-1].get("Mitigation.success") if rows else None
        found = sorted(out.rglob("artifact/fault.json"))
        if not found:
            c["status"] = "rejected"
            c["reason"] = "no artifact produced"
            return
        dest = self.work / "candidates" / job.cid
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(found[-1].parent, dest)
        artifact = FaultArtifact.load(dest)
        c["meta"] = artifact.meta
        problems = artifact.problems()
        leaks = artifact.leak_findings()
        c["lint"] = {"problems": problems, "leaks": leaks}
        if artifact.meta.get("app") != c["app"]:
            problems.append(f"artifact app {artifact.meta.get('app')} != {c['app']}")
        if problems:
            c["status"] = "rejected"
            c["reason"] = "artifact lint: " + "; ".join(problems)
            return
        # Script text is only a hint; the validation replay scans the cluster for real leaks.
        c["status"] = "validating"

    def run_validate(self, job: Job) -> None:
        c = self.cand(job.cid)
        pid = f"gen_{job.cid}"
        out = self.work / "runs" / job.cid / "validate"
        started = time.time()
        code = self.dind(
            f"val-{job.cid}", out,
            self.main_cmd(pid, "refmit", "none", self.o.replays, 1800, ["--internet-access", "open"]),
            {"CHAOS_ARTIFACT_ID": pid, "CHAOS_PERSISTENCE_S": str(self.o.persistence),
             "SREGYM_GENERATED_DIRS": "/opt/sregym/chaos-candidates"},
            [(self.work / "candidates" / job.cid, f"/opt/sregym/chaos-candidates/{job.cid}")],
            self.o.replays * 3600,
        )
        rows = self.csv_rows(out)
        injects = self.gate_reports(out, "inject")
        if not rows and not injects and self.infra_retry(c, out, "validating"):
            return
        mitigations = self.gate_reports(out, "mitigate")
        gates = {
            "replays": len(injects),
            "impact": [r.get("impact_pass", False) for r in injects],
            "observability": [r.get("observability_pass", False) for r in injects],
            "persistence": [r.get("persistence_pass", False) for r in injects],
            "integrity": [r.get("integrity_pass", False) for r in injects],
            "no_leak": [r.get("no_leak_pass", False) for r in injects],
            "leaks": [r.get("leaks", []) for r in injects],
            "new_alerts": [r.get("new_alerts", []) for r in injects],
            "mitigate_rc": [m.get("returncode") for m in mitigations],
            "solvability": [r.get("Mitigation.success") == "True" for r in rows],
        }
        checks = {
            "reproducibility": len(injects) >= self.o.replays and all(gates["impact"]),
            "impact": bool(gates["impact"]) and all(gates["impact"]),
            "observability": bool(gates["observability"]) and all(gates["observability"]),
            "persistence": bool(gates["persistence"]) and all(gates["persistence"]),
            "integrity": bool(gates["integrity"]) and all(gates["integrity"]),
            "no_leak": bool(gates["no_leak"]) and all(gates["no_leak"]),
            "solvability": len(gates["solvability"]) >= self.o.replays and all(gates["solvability"]),
        }
        c["validate"] = {"exit": code, "minutes": round((time.time() - started) / 60, 1), "gates": gates,
                         "checks": checks}
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            c["status"] = "rejected"
            c["reason"] = "gates failed: " + ", ".join(failed)
        else:
            c["status"] = "panel"
            c["panel"] = {"attempts": []}

    def run_panel(self, job: Job) -> None:
        c = self.cand(job.cid)
        pid = f"gen_{job.cid}"
        agent = job.args["agent"]
        rnd = job.args["round"]
        model = self.o.model if agent == "codex" else self.o.claude_model
        out = self.work / "runs" / job.cid / f"panel-{agent}-r{rnd}"
        started = time.time()
        code = self.dind(
            f"fix-{agent[:6]}-{rnd}-{job.cid}"[:63], out,
            self.main_cmd(pid, agent, model, 1, 1800, ["--force-build"]),
            {"SREGYM_GENERATED_DIRS": "/opt/sregym/chaos-candidates"},
            [(self.work / "candidates" / job.cid, f"/opt/sregym/chaos-candidates/{job.cid}")],
            3 * 3600, claude=agent == "claudecode",
        )
        rows = self.csv_rows(out)
        injects = self.gate_reports(out, "inject")
        live = bool(injects) and injects[-1].get("impact_pass", False)
        if not rows:
            shutil.rmtree(out, ignore_errors=True)  # environment failure; the round will be retried
        row = rows[-1] if rows else {}
        attempt = {
            "agent": agent, "round": rnd, "exit": code, "minutes": round((time.time() - started) / 60, 1),
            "valid": bool(rows) and live,
            "solved": row.get("Mitigation.success") == "True",
            "reason": row.get("Mitigation.reason"), "ttm": row.get("TTM"), "timed_out": row.get("timed_out"),
            "digest": fixer_digest(out),
        }
        with LOCK:
            c["panel"]["attempts"].append(attempt)
        self.log(f"PANEL {job.cid} {agent} r{rnd}: { {k: v for k, v in attempt.items() if k != 'digest'} }")

    # ------------------------------------------------------------- decisions
    def panel_agents(self) -> list[str]:
        agents = ["codex"]
        if self.o.claude_env_file:
            agents.append("claudecode")
        return agents

    def panel_jobs(self, cid: str) -> list[Job]:
        """Next panel jobs for a candidate (none while any are running)."""
        c = self.cand(cid)
        if any(j.cid == cid and j.kind == "panel" for j in self.running.values()):
            return []
        attempts = c["panel"]["attempts"]
        valid = [a for a in attempts if a["valid"]]
        solves = sum(a["solved"] for a in valid)
        total_target = self.o.k * len(self.panel_agents())
        max_solves = self.o.max_solves
        fast = [a for a in valid if a["solved"] and float(a.get("ttm") or 1e9) < self.o.fast_solve]
        if solves > max_solves or fast:
            c["status"] = "rejected"
            c["reason"] = f"too easy: {solves}/{len(valid)} solved" + (
                f", fastest in {min(float(a['ttm']) for a in fast) / 60:.0f} min" if fast else "")
            if c.get("depth", 0) < self.o.max_hardening:
                c["harden"] = "pending"
            return []
        jobs = []
        for agent in self.panel_agents():
            done = [a for a in attempts if a["agent"] == agent]
            done_valid = [a for a in done if a["valid"]]
            if len(done_valid) >= self.o.k:
                continue
            if len(done) - len(done_valid) >= 2:  # injection repeatedly not live: unreliable fault
                c["status"] = "rejected"
                c["reason"] = f"fault not live in {agent} attempts"
                return []
            jobs.append(Job("panel", cid, 0, f"{agent}", {"agent": agent, "round": len(done) + 1}))
        if not jobs and len(valid) >= total_target:
            c["status"] = "accepted"
            c["solve_rate"] = f"{solves}/{len(valid)}"
            self.accept(cid)
        return jobs

    def accept(self, cid: str) -> None:
        dest = GENERATED_ROOT / cid
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(self.work / "candidates" / cid, dest)
        c = self.cand(cid)
        meta = json.loads((dest / "fault.json").read_text())
        meta["generation"] = {
            "chaos_backend": c["chaos_backend"], "theme": c["theme"][0], "solve_rate": c["solve_rate"],
            "panel": c["panel"]["attempts"], "validated": c["validate"]["checks"], "accepted": now(),
        }
        (dest / "fault.json").write_text(json.dumps(meta, indent=2) + "\n")
        self.log(f"ACCEPTED {cid} solve_rate={c['solve_rate']} -> {dest}")

    def new_candidate(self) -> Job:
        n = self.ledger["sessions"]
        self.ledger["sessions"] += 1
        for pid, parent in self.ledger["candidates"].items():
            if parent.get("harden") == "pending":
                parent["harden"] = "spawned"
                depth = parent.get("depth", 0) + 1
                cid = f"{datetime.datetime.now():%m%d}{n:03d}_{parent['app'].split('_')[0]}_h{depth}"
                self.ledger["candidates"][cid] = {
                    "app": parent["app"], "theme": parent["theme"], "chaos_backend": parent["chaos_backend"],
                    "status": "chaos", "created": now(), "parent": pid, "depth": depth,
                }
                return Job("chaos", cid, 2, f"harden {pid}")
        app = APPS[n % len(APPS)]
        themes = [t for t in THEMES if not (t[0] == "code" and app != "social_network")]
        theme = themes[(n // len(APPS)) % len(themes)]
        backend = "codex"
        if self.o.claude_env_file and self.o.chaos_claude_every and n % self.o.chaos_claude_every == 1:
            backend = "claudecode"
        cid = f"{datetime.datetime.now():%m%d}{n:03d}_{app.split('_')[0]}_{theme[0].replace('-', '')}"
        self.ledger["candidates"][cid] = {"app": app, "theme": list(theme), "chaos_backend": backend,
                                          "status": "chaos", "created": now()}
        return Job("chaos", cid, 2, f"{app}/{theme[0]}")

    def pending_jobs(self) -> list[Job]:
        jobs = []
        for cid, c in self.ledger["candidates"].items():
            busy = any(j.cid == cid for j in self.running.values())
            if c["status"] == "validating" and not busy:
                jobs.append(Job("validate", cid, 1, "validate"))
            elif c["status"] == "panel":
                jobs += self.panel_jobs(cid)
            elif c["status"] == "chaos" and not busy:
                jobs.append(Job("chaos", cid, 2, "resume"))
        return sorted(jobs, key=lambda j: j.priority)

    # ------------------------------------------------------------------ loop
    def worker(self, job: Job, key: str) -> None:
        try:
            {"chaos": self.run_chaos, "validate": self.run_validate, "panel": self.run_panel}[job.kind](job)
        except Exception as exc:  # keep the pipeline alive
            self.log(f"ERROR {job.kind} {job.cid}: {exc!r}")
            if job.kind != "panel":
                self.cand(job.cid)["status"] = "error"
                self.cand(job.cid)["reason"] = repr(exc)
        finally:
            with LOCK:
                self.running.pop(key, None)
                c = self.cand(job.cid)
                self.log(f"END {job.kind} {job.cid} -> {c['status']} {c.get('reason', '')}")
                self.save()

    def stop_requested(self) -> bool:
        return (self.work / "STOP").exists()

    def generating(self) -> bool:
        return not self.stop_requested() and self.ledger["sessions"] < self.o.max_sessions

    def apply_requeue(self) -> None:
        """Apply lines "<cid> <status>" from <work>/requeue (operator overrides), then delete it."""
        path = self.work / "requeue"
        if not path.exists():
            return
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in self.ledger["candidates"]:
                c = self.cand(parts[0])
                c["status"] = parts[1]
                phase = {"validating": "validate", "chaos": "chaos"}.get(parts[1])
                if phase:
                    shutil.rmtree(self.work / "runs" / parts[0] / phase, ignore_errors=True)
                c.pop("reason", None)
                if parts[1] == "panel":
                    c.setdefault("panel", {"attempts": []})
                self.log(f"REQUEUE {parts[0]} -> {parts[1]}")
        path.unlink()

    def run(self) -> None:
        self.log(f"pipeline start slots={self.o.slots} model={self.o.model} panel={self.panel_agents()} k={self.o.k}")
        seq = 0
        while True:
            with LOCK:
                self.apply_requeue()
                free = self.o.slots - len(self.running)
                jobs = self.pending_jobs() if free > 0 else []
                in_flight = sum(1 for c in self.ledger["candidates"].values()
                                if c["status"] in ("chaos", "validating", "panel"))
                if free > 0 and not jobs and self.generating() and in_flight < self.o.slots + 1:
                    jobs = [self.new_candidate()]
                for job in jobs[:max(free, 0)]:
                    seq += 1
                    key = f"{job.kind}-{job.cid}-{seq}"
                    self.running[key] = job
                    self.log(f"START {job.kind} {job.cid} {job.label}")
                    threading.Thread(target=self.worker, args=(job, key), daemon=True).start()
                self.save()
                idle = not self.running and not self.pending_jobs() and not self.generating()
            if idle:
                break
            time.sleep(20 if self.running else 5)
        self.log("pipeline done")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--image", default="sregym-dind:local")
    parser.add_argument("--slots", type=int, default=3, help="concurrent DinD environments")
    parser.add_argument("--cpus", type=float, default=5)
    parser.add_argument("--memory", default="18g")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--claude-model", default="claude-opus-5-5")
    parser.add_argument("--claude-env-file", type=Path, help="env file with CLAUDE_CODE_OAUTH_TOKEN; enables Claude")
    parser.add_argument("--chaos-claude-every", type=int, default=0, help="every Nth chaos session uses Claude")
    parser.add_argument("--k", type=int, default=2, help="attempts per panel agent")
    parser.add_argument("--max-solves", type=int, default=1, help="accept when at most this many attempts solve")
    parser.add_argument("--fast-solve", type=float, default=900,
                        help="reject at once when a valid attempt solves within this many seconds")
    parser.add_argument("--infra-retries", type=int, default=2, help="re-runs after environment failures")
    parser.add_argument("--max-hardening", type=int, default=2, help="hardening generations per lineage")
    parser.add_argument("--replays", type=int, default=2, help="fresh-deployment replays in validation")
    parser.add_argument("--persistence", type=int, default=300, help="seconds the fault must persist unattended")
    parser.add_argument("--chaos-timeout", type=int, default=4200)
    parser.add_argument("--max-sessions", type=int, default=1000)
    parser.add_argument("--registry-mirrors", default="http://172.17.0.1:5000")
    opts = parser.parse_args()
    opts.work = opts.work.resolve()
    opts.work.mkdir(parents=True, exist_ok=True)
    Pipeline(opts).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
