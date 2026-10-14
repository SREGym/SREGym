#!/usr/bin/env python3
"""Run SREGym problems in parallel DinD environments, one problem (N attempts) per container.

  python3 docker/dind/campaign.py --image sregym-dind:local --out results/campaign --jobs 3 \
      --model gpt-6-sol --attempts 3 problem_a problem_b ...

Each problem gets its own outer container (private Docker daemon + kind cluster), a private copy of
the Codex subscription credentials at /root/.codex/auth.json, and a results directory
<out>/<problem><suffix>/<run-id>/. Each retry uses fresh storage. A problem is
complete only when its container exits successfully and records all N complete attempts.

Campaigns that run at the same time need distinct --name-prefix values, or one removes the other's
containers. --max-containers-file caps the DinD containers running host-wide across all of them.
"""

import argparse
import concurrent.futures
import contextlib
import csv
import datetime
import fcntl
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from run import run_command

AUTH = Path.home() / ".codex" / "auth.json"
LOCK = threading.Lock()
SLOT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with LOCK:
        print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def running_containers() -> list[str] | None:
    proc = subprocess.run(["docker", "ps", "--format", "{{.Image}} {{.Names}}"], capture_output=True, text=True)
    return proc.stdout.split("\n") if proc.returncode == 0 else None


def wait_for_slot(args) -> None:
    """Block until fewer DinD containers run host-wide than the limit in --max-containers-file."""
    repo = args.image.split(":")[0] + ":"
    while args.max_containers_file:
        try:
            limit = int(args.max_containers_file.read_text().strip())
        except (OSError, ValueError):
            return
        rows = running_containers()
        # An unreadable `docker ps` counts as full, never as empty.
        if rows is not None and sum(row.startswith(repo) for row in rows) < limit:
            return
        time.sleep(30)


@contextlib.contextmanager
def slot_lock(args):
    """Serialize slot checks and container starts across threads and, via a file lock, campaigns."""
    with SLOT_LOCK:
        if not args.max_containers_file:
            yield
            return
        with open(args.max_containers_file.with_suffix(".lock"), "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def wait_until_listed(name: str, proc: subprocess.Popen, timeout_s: float = 300) -> None:
    """Return once container ``name`` shows in `docker ps` (or its run already ended)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and proc.poll() is None:
        if any(row.split(" ")[-1] == name for row in running_containers() or [] if row):
            return
        time.sleep(2)


def result_csvs(out: Path) -> list[Path]:
    return sorted(p for p in out.rglob("*_results.csv") if not p.name.startswith("_running_"))


def complete_attempts(files: list[Path], problem: str) -> set[int]:
    states: dict[int, set[str]] = {}
    for path in files:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("problem_id") != problem:
                    continue
                try:
                    attempt = int(row.get("attempt", ""))
                except ValueError:
                    continue
                states.setdefault(attempt, set()).add(row.get("run_status", ""))
    return {attempt for attempt, values in states.items() if values == {"complete"}}


def _run_attempt(problem, args, run_dir: Path, auth_copy: Path, name: str) -> tuple[int, float]:
    payload = [
        "uv",
        "run",
        "--frozen",
        "main.py",
        "--problem",
        problem,
        "--agent",
        "codex",
        "--model",
        args.model,
        "--judge-backend",
        "codex",
        "--n-attempts",
        str(args.attempts),
        *args.extra,
    ]
    launch = argparse.Namespace(
        name=name,
        output=run_dir,
        cpus=args.cpus,
        memory=args.memory,
        docker_tmpfs_size=None,
        env_file=None,
        image=args.image,
        command=payload,
    )
    environment = list(args.env)
    if args.registry_mirrors:
        environment.append(f"SREGYM_REGISTRY_MIRRORS={args.registry_mirrors}")
    command = run_command(
        launch,
        mounts=(f"type=bind,src={auth_copy},dst=/root/.codex/auth.json",),
        environment=environment,
        forward_credentials=False,
    )
    with open(run_dir / "campaign.log", "w") as handle:
        proc = None
        try:
            with slot_lock(args):
                wait_for_slot(args)
                started = time.monotonic()
                proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
                wait_until_listed(name, proc)
            code = proc.wait()
        except BaseException:
            if proc is not None:
                subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
                proc.wait(timeout=60)
            raise
    return code, (time.monotonic() - started) / 60


def run_problem(problem: str, args) -> dict:
    out = (args.out / f"{problem}{args.suffix}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    history = []
    for attempt in range(1, args.retries + 2):
        run_id = uuid.uuid4().hex[:12]
        run_dir = out / run_id
        run_dir.mkdir()
        name = f"{args.name_prefix}-{problem}"[:50].replace("_", "-") + f"-{run_id}"
        log(f"START {problem} (try {attempt}) -> {run_dir}")
        with tempfile.TemporaryDirectory(prefix="sregym-codex-") as credentials:
            auth_copy = Path(credentials) / "auth.json"
            shutil.copyfile(AUTH, auth_copy)
            auth_copy.chmod(0o600)
            code, minutes = _run_attempt(problem, args, run_dir, auth_copy, name)
        csvs = result_csvs(run_dir)
        complete = complete_attempts(csvs, problem)
        success = code == 0 and complete == set(range(1, args.attempts + 1))
        history.append(
            {"exit": code, "minutes": minutes, "complete_attempts": len(complete), "directory": str(run_dir)}
        )
        log(f"END {problem} (try {attempt}) exit={code} {minutes:.0f}min csv={[str(c) for c in csvs]}")
        result = {
            "problem": problem,
            "success": success,
            "exit": code,
            "minutes": minutes,
            "complete_attempts": len(complete),
            "requested_attempts": args.attempts,
            "csv": [str(c) for c in csvs],
            "runs": history,
        }
        if success:
            return result
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("problems", nargs="+")
    parser.add_argument("--image", default="sregym-dind:local")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--cpus", type=float, default=6)
    parser.add_argument("--memory", default="18g")
    parser.add_argument("--model", default="gpt-6-sol")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--registry-mirrors", default="", help="comma-separated Docker Hub mirrors for each run")
    parser.add_argument("--name-prefix", default="camp", help="container name prefix; distinct per concurrent campaign")
    parser.add_argument("--suffix", default="", help="results directory suffix, e.g. .topup1 for extra attempts")
    parser.add_argument("--env", action="append", default=[], help="KEY=VALUE passed to each run (repeatable)")
    parser.add_argument("--max-containers-file", type=Path, help="file holding the host-wide DinD container limit")
    parser.add_argument("--extra", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    if args.attempts < 1 or args.jobs < 1 or args.retries < 0:
        parser.error("attempts and jobs must be positive; retries must be nonnegative")
    args.out.mkdir(parents=True, exist_ok=True)
    summary = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {}
        for index, problem in enumerate(args.problems):
            futures[pool.submit(run_problem, problem, args)] = problem
            if index < args.jobs:
                time.sleep(60)  # stagger cold starts (image pulls, kind boot)
        for future in concurrent.futures.as_completed(futures):
            try:
                summary.append(future.result())
            except Exception as exc:  # keep the campaign going
                summary.append({"problem": futures[future], "error": repr(exc)})
            (args.out / f"campaign-summary{args.suffix}.json").write_text(json.dumps(summary, indent=2))
    log("CAMPAIGN DONE")
    return 0 if all(result.get("success", False) for result in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
