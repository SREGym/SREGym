"""Host-side lifecycle for the saved company world; never imported by an agent.

The normal conductor owns the run. This helper provides its independent world
cutoff and uses the existing world's QMP and process-family implementation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# Customer endpoints whose service the grader judges at the deadline.
HEALTH_ENDPOINTS = {
    "eu-health": "http://127.0.0.1:28310/health",
    "us-health": "http://127.0.0.1:28320/health",
    "eu-live": "http://127.0.0.1:28310/api/live",
    "us-live": "http://127.0.0.1:28320/api/live",
}


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(".publishing")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def modules(project):
    sys.path.insert(0, str(Path(project) / "build"))
    return tuple(importlib.import_module(name) for name in ("lab", "revive", "world_policy", "world_run_guard"))


def require_owner(run, policy):
    config = policy.config() or {}
    if Path(config.get("probe_run", "/")) != Path(run):
        raise RuntimeError("Company world ownership changed; refusing to operate on another run")
    return config


def ready(project, run):
    lab, revive, policy, _guard = modules(project)
    require_owner(run, policy)
    inventory = subprocess.check_output([str(project / "build/companyctl"), "inventory"], text=True, timeout=30)
    rows = json.loads(inventory)["servers"]
    names = {row["name"] for row in rows}
    expected = {
        f"{region}-{role}" for region in ("eu", "us") for role in ("parent-01", "work-01", "work-02", "work-03")
    }
    if not expected <= names:
        raise RuntimeError("Company inventory is incomplete: " + ", ".join(sorted(expected - names)))
    for port in (28310, 28320, 28311, 28330, 28390, 28391, 28392):
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    saved = json.loads((run / "prepared.json").read_text())
    for name in saved["live"]:
        status = revive.result(name, "query-status")
        if not status["running"]:
            raise RuntimeError(f"Saved-live guest {name} is not running")
    start_resource_guard(lab, run)
    write(run / "readiness.json", {"inventory": sorted(names), "saved_live": saved["live"], "ready": True})


def resource_path(lab):
    config = lab.ROOT / "world-config.json"
    subdir = json.loads(config.read_text()).get("artifact_subdir", "runs") if config.exists() else "runs"
    return lab.ARTIFACTS / subdir / "resource-current.json"


def start_resource_guard(lab, run):
    """Arm the bundle's resource watcher after revive and before T0."""
    started = time.time()
    lab.start_background(
        "sregym-world-guard",
        lab.VM / "world-guard-sregym.pid",
        [sys.executable, str(lab.ROOT / "world_guard.py")],
        "sregym-world-guard.log",
    )
    end = time.monotonic() + 10
    resource = resource_path(lab)
    while time.monotonic() < end:
        pid = lab.pid_of(lab.VM / "world-guard-sregym.pid")
        if not pid or not lab.owned(pid, "world_guard.py"):
            raise RuntimeError("Host resource watcher did not remain running")
        try:
            row = json.loads(resource.read_text())
            if row["epoch"] >= started:
                if row.get("stop_reasons"):
                    raise RuntimeError("Host resource preflight failed: " + ", ".join(row["stop_reasons"]))
                if not row.get("CPU_PSI_and_load_are_telemetry_only"):
                    raise RuntimeError("Resource watcher must treat CPU pressure and load as telemetry")
                write(run / "resource-guard.json", {"pid": pid, "started_host_epoch": started, "initial_sample": row})
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise TimeoutError("Host resource watcher produced no fresh initial sample")


def resource_problem(lab):
    pid = lab.pid_of(lab.VM / "world-guard-sregym.pid")
    if not pid or not lab.owned(pid, "world_guard.py"):
        return "Host resource watcher exited"
    try:
        row = json.loads(resource_path(lab).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if row.get("stop_reasons"):
        return "Host resource limit: " + ", ".join(row["stop_reasons"])
    if time.time() - row["epoch"] > 10:
        return "Host resource watcher stopped reporting"
    return None


def stop_agent(run):
    record = {"begin_monotonic": time.monotonic()}
    path = run / "agent-container.json"
    if path.exists():
        name = json.loads(path.read_text())["name"]
        if not name.startswith("sregym-") or not all(c.isalnum() or c in "-_" for c in name):
            raise ValueError("Invalid run container name")
        result = subprocess.run(["docker", "kill", name], capture_output=True, text=True, timeout=15)
        record.update(container=name, returncode=result.returncode, error=result.stderr.strip())
    else:
        record["agent_not_started"] = True
    record["completed_monotonic"] = time.monotonic()
    write(run / "solver-cutoff.json", record)


def freeze(project, run):
    lab, revive, policy, guard = modules(project)
    cfg = require_owner(run, policy)
    launch = json.loads((run / "launch.json").read_text())
    cutoff = {
        "target_monotonic": launch["start_monotonic"] + launch["seconds"],
        "cutoff_begin_monotonic": time.monotonic(),
        "host_epoch": time.time(),
        "guests": {},
    }
    families = {}
    errors = []
    # Stop the agent independently of the driver's polling interval. The
    # company gateway also refuses new actions once the monotonic budget ends.
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        agent = pool.submit(stop_agent, run)
        for label, expected in (("world-tools", "world_tools.py"), ("provider", "provider.py")):
            pid = lab.pid_of(lab.VM / (label + ".pid"))
            if pid and lab.owned(pid, expected):
                try:
                    families[label] = guard.freeze_family(pid, expected)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
        names = [
            p.stem for p in lab.VM.glob("*.pid") if (pid := lab.pid_of(p)) and lab.owned(pid, "qemu-system-x86_64")
        ]

        def pause(name):
            result = revive.result(name, "stop")
            status = revive.result(name, "query-status")
            if status["status"] != "paused":
                raise RuntimeError(f"{name} did not pause: {status}")
            return name, {"at_monotonic": time.monotonic(), "result": result, "status": status}

        futures = {pool.submit(pause, name): name for name in names}
        for future, name in futures.items():
            try:
                key, value = future.result()
                cutoff["guests"][key] = value
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        cutoff["freeze_completed_monotonic"] = time.monotonic()
        cutoff["host_processes_stopped"] = families
        cutoff["errors"] = errors
        write(run / "native-cutoff.json", cutoff)
        agent.result()
    if errors:
        raise RuntimeError("Incomplete deadline freeze: " + "; ".join(errors))
    capsule = run / "end-state"
    capsule.mkdir(mode=0o700)
    (capsule / "ram").mkdir()
    (capsule / "disks").mkdir()
    # A paused QEMU exports native RAM/device state without advancing guests.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda n: revive.export_native(n, capsule / "ram" / (n + ".gz")), names))
    for name in names:
        lab.stop_pid(lab.VM / (name + ".pid"), "qemu-system-x86_64", 2)
    public = json.loads((lab.ROOT / "public-instances.json").read_text())
    disks = {**cfg["fronts"], "ops-helper-01": cfg["private_client_disk"], **{n: v["disk"] for n, v in public.items()}}
    for name in names:
        if name.endswith("-" + lab.RESCUE_MARK):
            disks[name] = str(lab.VM / cfg["disk_subdir"] / (name + ".qcow2"))
    retained = {}
    for name, source in disks.items():
        source, target = Path(source), capsule / "disks" / (name + ".qcow2")
        if not source.exists():
            continue  # Agent-deleted instances may no longer have a disk.
        if source.is_relative_to(run):
            source.rename(target)
        else:
            subprocess.run(["cp", "--reflink=auto", "--sparse=always", str(source), str(target)], check=True)
        target.chmod(0o400)
        retained[name] = str(target)
    manifest = {
        "live": names,
        "allnames": list(retained),
        "disks": retained,
        "ram": {n: str(capsule / "ram" / (n + ".gz")) for n in names},
        "cutoff": cutoff,
        "world_config": cfg,
        "public_instances": public,
        "console_pending": {p.name: p.read_text() for p in lab.VM.glob("*.console-pending")},
        "run_identity": run.name,
        "no_post_deadline_actions_credited": True,
    }
    write(capsule / "config.json", cfg)
    write(capsule / "manifest.json", manifest)
    write(run / "native-capture-complete.json", {"capture": str(capsule), "completed_monotonic": time.monotonic()})


def teardown(project, run):
    lab, revive, policy, guard = modules(project)
    config = require_owner(run, policy)
    lab.stop_pid(lab.VM / "world-guard-sregym.pid", "world_guard.py", 2)
    cutoff = run / "native-cutoff.json"
    if cutoff.exists():
        for family in json.loads(cutoff.read_text()).get("host_processes_stopped", {}).values():
            guard.stop_frozen_family(family)
    revive.stop_owned()
    # These belong to the world infrastructure started by revive. Never stop
    # services in a shared slice or use name-wide pkill/systemctl commands.
    seed = "company_seed_http.py" if config.get("world_cycle") == 2 else "http.server"
    for label, expected in (("switch", "switch.py"), ("seed", seed)):
        lab.stop_pid(lab.VM / (label + ".pid"), expected, 2)
    write(run / "teardown.json", {"completed_monotonic": time.monotonic(), "saved_capture_retained": True})


def probe_endpoints(run):
    """Record whether each customer endpoint serves just before the deadline freeze."""

    def get(url):
        began = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                body = response.read(400).decode("utf-8", "replace")
                return {"status": response.status, "seconds": time.monotonic() - began, "body": body}
        except Exception as exc:
            return {"status": getattr(exc, "code", None), "seconds": time.monotonic() - began, "error": repr(exc)[:300]}

    record = {"begin_host_epoch": time.time(), "begin_monotonic": time.monotonic()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(HEALTH_ENDPOINTS)) as pool:
        record["endpoints"] = dict(zip(HEALTH_ENDPOINTS, pool.map(get, HEALTH_ENDPOINTS.values()), strict=True))
    record["completed_monotonic"] = time.monotonic()
    write(run / "service-health-at-D.json", record)


def watch(project, run, parent_pid):
    launch = json.loads((run / "launch.json").read_text())
    deadline = launch["start_monotonic"] + launch["seconds"]
    write(run / "watch-ready.json", {"pid": os.getpid(), "deadline_monotonic": deadline})
    lab, _revive, _policy, _guard = modules(project)
    done = threading.Event()

    def sample_clock():
        lab, _revive, _policy, _guard = modules(project)
        while not done.is_set() and time.monotonic() < deadline:
            try:
                before = time.monotonic_ns()
                native = int(lab.ssh("ops-helper-01", "date +%s%N", timeout=10).strip())
                after = time.monotonic_ns()
                row = {
                    "rows": [
                        {
                            "private_clock": [{"native_epoch_ns": native}],
                            "host_before": {"host_monotonic_ns": before},
                            "host_after": {"host_monotonic_ns": after},
                        }
                    ]
                }
                with (run / "guest-health.jsonl").open("a") as stream:
                    stream.write(json.dumps(row) + "\n")
            except Exception:
                pass  # Missing clock bounds remain ungraded in the oracle.
            done.wait(30)

    threading.Thread(target=sample_clock, daemon=True).start()
    prober = threading.Thread(target=probe_endpoints, args=(run,), daemon=True)
    next_resource_check = 0
    while time.monotonic() < deadline:
        if prober.ident is None and time.monotonic() >= deadline - 6:
            prober.start()
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            write(run / "abort.json", {"reason": "Conductor exited before the deadline"})
            break
        if (run / "abort.json").exists():
            break
        if time.monotonic() >= next_resource_check:
            issue = resource_problem(lab)
            if issue:
                write(run / "abort.json", {"reason": issue, "host_epoch": time.time()})
                break
            next_resource_check = time.monotonic() + 1
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    done.set()
    if prober.ident is not None:
        prober.join(max(0, deadline - time.monotonic()))  # Never delays the freeze past the deadline.
    try:
        freeze(project, run)
    except Exception as exc:
        write(run / "capture-error.json", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    if (run / "abort.json").exists():
        teardown(project, run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("watch", "teardown", "ready", "freeze"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    if args.action == "watch":
        watch(args.project, args.run, args.parent_pid)
    elif args.action == "teardown":
        teardown(args.project, args.run)
    elif args.action == "ready":
        ready(args.project, args.run)
    else:
        freeze(args.project, args.run)
