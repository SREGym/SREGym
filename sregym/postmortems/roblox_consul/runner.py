"""Host-side lifecycle, independent grader, and oracle for the Roblox family.

Usage: python3 -m sregym.postmortems.roblox_consul --help
Only the agent toolbox and two operational HTTP ports are exposed. Every run
has its own Compose project, networks, volumes, random runner token, and traces.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from .common import request
from .grading import evaluate
from .model import TIERS, expected_player

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def docker(*args, capture=True, check=True, timeout=600):
    command = (["sudo"] if os.geteuid() else []) + ["docker", *map(str, args)]
    return subprocess.run(command, check=check, text=True, capture_output=capture, timeout=timeout)


class Run:
    def __init__(self, name):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
            raise ValueError("run name must be a lowercase identifier, up to 40 characters")
        self.name = name
        self.root = REPO / "results" / "roblox-consul" / name
        self.project = "sregym-roblox-" + name
        self.compose = self.root / "compose.json"

    def dc(self, *args, **kwargs):
        return docker("compose", "-p", self.project, "-f", self.compose, *args, **kwargs)

    def metadata(self):
        return json.loads((self.root / "run.json").read_text())

    def url(self, service):
        line = self.dc("port", service, "8080").stdout.strip().splitlines()[0]
        return "http://" + line

    def ops(self, action, **args):
        return request(self.url("control") + "/ops", "POST", {"action": action, "args": args}, timeout=40)

    def private(self, endpoint, **data):
        token = (self.root / "runner-token").read_text().strip()
        return request(
            self.url("control") + "/runner/" + endpoint,
            "POST",
            data,
            headers={"Authorization": "Bearer " + token},
            timeout=30,
        )

    def up(self, tier, mode, seed):
        if self.compose.exists():
            raise ValueError("run already exists; use down then a new name, or reset")
        self.root.mkdir(parents=True, exist_ok=True)
        token_file = self.root / "runner-token"
        token_file.write_text(secrets.token_hex(32))
        token_file.chmod(0o600)
        shutil.copyfile(HERE / "briefing.md", self.root / "briefing.md")
        services = {}
        volumes = {"gateway": {}, "control": {}, "database": {}, "cache-a": {}, "cache-b": {}}
        for i in range(1, 4):
            name = f"consul-{i}"
            volumes[name] = {}
            services[name] = {
                "image": "hashicorp/consul:1.15.4@sha256:3687a77458411c20dc099c5b955d221669b0b7c2fc8c8a2673eca4047df2c3cd",
                "command": [
                    "agent",
                    "-server",
                    "-bootstrap-expect=3",
                    f"-node={name}",
                    "-client=0.0.0.0",
                    "-retry-join=consul-1",
                    "-retry-join=consul-2",
                    "-retry-join=consul-3",
                    "-data-dir=/consul/data",
                ],
                "environment": {"CONSUL_LOCAL_CONFIG": '{"disable_update_check":true,"log_level":"warn"}'},
                "networks": ["backend"],
                "volumes": [f"{name}:/consul/data"],
                "mem_limit": "512m",
            }
        services["database"] = {
            "image": "postgres:16.10-bookworm@sha256:38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74",
            "environment": {"POSTGRES_PASSWORD": "lab-only", "POSTGRES_DB": "platform"},
            "networks": ["backend"],
            "volumes": ["database:/var/lib/postgresql/data"],
            "mem_limit": "512m",
        }
        for name in ("cache-a", "cache-b"):
            services[name] = {
                "image": "redis:7.2.10-bookworm@sha256:8cd70b529aec2af55a072917098c1ae6f1d9eb975ac1cf36928c7d5af7574f08",
                "command": ["redis-server", "--appendonly", "yes"],
                "networks": ["backend"],
                "volumes": [f"{name}:/data"],
                "mem_limit": "256m",
            }
        for name in ("control", "gateway"):
            services[name] = {
                "build": {"context": str(HERE)},
                "image": self.project + ":runtime",
                "command": ["python", f"{name}.py"],
                "environment": {"TIER": tier, "DATABASE_URL": "postgresql://postgres:lab-only@database/platform"},
                "networks": ["backend", "ops", "management"],
                "ports": ["127.0.0.1::8080"],
                "mem_limit": "1g",
            }
        del services["gateway"]["build"]
        services["control"]["volumes"] = ["control:/state", f"{token_file}:/run/secrets/runner_token:ro"]
        services["gateway"]["volumes"] = ["gateway:/state"]
        services["toolbox"] = {
            "build": {"context": str(HERE), "dockerfile": "Dockerfile.toolbox"},
            "image": self.project + ":toolbox",
            "command": ["sleep", "infinity"],
            "networks": ["ops"],
            "working_dir": "/workspace",
            "user": "1000:1000",
            "volumes": [
                f"{HERE / 'ops.py'}:/usr/local/bin/ops:ro",
                f"{self.root / 'briefing.md'}:/workspace/briefing.md:ro",
            ],
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": "1g",
        }
        self.compose.write_text(
            json.dumps(
                {
                    "services": services,
                    "volumes": volumes,
                    "networks": {"backend": {"internal": True}, "ops": {"internal": True}, "management": {}},
                },
                indent=2,
            )
        )
        meta = {
            "family": "roblox-consul-2021",
            "tier": tier,
            "mode": mode,
            "seed": seed,
            "created_at": time.time(),
            "fidelity": "hybrid: real Consul/PostgreSQL/Redis; modeled scale defects and scheduler",
            "project": self.project,
            "source_sha256": {
                name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                for name in (
                    "Dockerfile",
                    "Dockerfile.toolbox",
                    "control.py",
                    "gateway.py",
                    "model.py",
                    "common.py",
                    "ops.py",
                    "briefing.md",
                    "grading.py",
                    "runner.py",
                )
            },
        }
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))
        try:
            self.dc("build", capture=False)
            self.dc("up", "-d", "--no-build", capture=False)
            control = self.url("control")
            for _ in range(120):
                try:
                    if request(control + "/health", timeout=1)["ready"]:
                        break
                except Exception:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("control readiness timed out; inspect logs")
            time.sleep(4)
            baseline = self.grade(window=5, challenge=False, baseline=True)
            if not baseline["passed"]:
                raise RuntimeError("baseline verification failed; see baseline.json")
            print(json.dumps({"run": self.name, "ready": True, "control": control, "gateway": self.url("gateway")}))
        except Exception:
            self.export()
            raise

    def inject(self):
        meta = self.metadata()
        meta["gateway_at_injection"] = request(self.url("gateway") + "/metrics")
        result = self.private("inject", mode=meta["mode"], seed=meta["seed"])
        meta["injected_at"] = time.time()
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))
        return result

    def grade(self, window=15, challenge=True, baseline=False):
        try:
            result = self._grade(window=window, challenge=challenge, baseline=baseline)
            result["valid"] = True
        except (urllib.error.URLError, TimeoutError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
            # A broken evaluator must never silently become a successful task
            # or an ordinary agent failure in aggregate reliability statistics.
            result = {"passed": False, "valid": False, "error": type(exc).__name__, "detail": str(exc)}
            self.export()
        filename = "baseline.json" if baseline else "grade.json"
        (self.root / filename).write_text(json.dumps(result, indent=2))
        return result

    def _grade(self, window=15, challenge=True, baseline=False):
        if window < 5:
            raise ValueError("grading window must be at least 5 seconds")
        meta = self.metadata()
        if not baseline and "injected_at" not in meta:
            raise ValueError("inject the incident before grading")
        gateway = self.url("gateway")
        start = time.time()
        if challenge:
            # Test actual leader transitions, not a required command sequence.
            for node in ("consul-1", "consul-2", "consul-3"):
                self.ops("transfer", node=node)
                time.sleep(window / 3)
        else:
            time.sleep(window)
        finish = time.time()
        evidence = self.private("evidence")
        samples = [s for s in evidence["samples"] if start + 1 <= s["time"] <= finish]
        metrics = request(gateway + "/metrics")
        rows = json.loads(
            self.dc(
                "exec",
                "-T",
                "database",
                "psql",
                "-U",
                "postgres",
                "-d",
                "platform",
                "-Atc",
                "SELECT coalesce(json_agg(p ORDER BY id),'[]') FROM players p",
            ).stdout
        )
        expected = [expected_player(i) for i in range(TIERS[meta["tier"]]["players"])]
        initial = meta.get("gateway_at_injection", {}).get("origin_overloads", 0)
        result = evaluate(
            samples,
            window=window,
            rps=TIERS[meta["tier"]]["rps"],
            rows=rows,
            expected=expected,
            overloads=metrics["origin_overloads"],
            initial_overloads=initial,
            voters=len([p for p in self.ops("raft") if p["Voter"]]),
            prior_incorrect=any(
                s["incorrect"] for s in evidence["samples"] if s["time"] >= meta.get("injected_at", start)
            ),
        )
        result.update(
            samples=len(samples),
            start=start,
            finish=finish,
            gateway=metrics,
            leader_challenge=challenge,
            tier=meta["tier"],
            player_data_sha256=hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        )
        filename = "baseline.json" if baseline else "grade.json"
        (self.root / filename).write_text(json.dumps(result, indent=2))
        self.export()
        return result

    def oracle(self):
        self.ops("config", changes={"admission_percent": 0, "streaming": False, "health_interval": 10})
        for node in ("consul-1", "consul-2", "consul-3"):
            if self.ops("profile", node=node)["bolt_free_pages"]:
                target = next(n for n in ("consul-1", "consul-2", "consul-3") if n != node)
                self.ops("transfer", node=target)
                self.ops("compact", node=node)
        self.ops("drain", node="cache-dead")
        self.ops("reconcile")
        for offset in range(0, TIERS[self.metadata()["tier"]]["players"], 20):
            self.ops("cache-warm", offset=offset, batch=20)
        for percentage in (10, 25, 50, 75, 100):
            self.ops("config", changes={"admission_percent": percentage})
            time.sleep(2)
        self.ops(
            "status-update",
            text="Discovery, allocation reconciliation, and cache validation complete. Player access restored progressively; checking sustained joins and leader changes.",
        )
        return self.grade()

    def export(self):
        try:
            (self.root / "containers.log").write_text(self.dc("logs", "--no-color", check=False).stdout)
            for file in ("events.jsonl", "telemetry.jsonl"):
                output = self.dc("exec", "-T", "control", "cat", f"/state/{file}", check=False)
                if output.returncode == 0:
                    (self.root / file).write_text(output.stdout)
            images = self.dc("images", "--format", "json", check=False)
            (self.root / "images.json").write_text(images.stdout)
        except Exception as exc:
            print(f"artifact export warning: {exc}", file=sys.stderr)

    def down(self):
        self.export()
        self.dc("down", "--volumes", "--remove-orphans", capture=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="demo")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--tier", choices=TIERS, default="small")
    up.add_argument("--mode", choices=("historical", "intervention"), default="historical")
    up.add_argument("--seed", type=int, default=0)
    for command in ("inject", "oracle", "status", "export", "down", "reset", "shell"):
        sub.add_parser(command)
    grade = sub.add_parser("grade")
    grade.add_argument("--window", type=int, default=15)
    op = sub.add_parser("ops")
    op.add_argument("action")
    op.add_argument("args", nargs="?", default="{}")
    bench = sub.add_parser("benchmark")
    bench.add_argument("--codex-binary", default=str(Path.home() / ".local/bin/codex"))
    bench.add_argument("--auth-file", default=str(Path.home() / ".codex/auth.json"))
    bench.add_argument("--model")
    bench.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    run = Run(args.run)
    try:
        if args.command == "up":
            run.up(args.tier, args.mode, args.seed)
        elif args.command == "inject":
            print(json.dumps(run.inject(), indent=2))
        elif args.command in {"grade", "oracle"}:
            result = run.oracle() if args.command == "oracle" else run.grade(window=args.window)
            print(json.dumps(result, indent=2))
            return 0 if result["passed"] else 1
        elif args.command == "benchmark":
            from .benchmark import benchmark

            result = benchmark(
                run, codex_binary=args.codex_binary, auth_file=args.auth_file, model=args.model, timeout=args.timeout
            )
            print(json.dumps(result, indent=2))
            return 0 if result["grade"]["passed"] else 1
        elif args.command == "status":
            print(json.dumps(run.ops("status"), indent=2))
        elif args.command == "ops":
            print(json.dumps(run.ops(args.action, **json.loads(args.args)), indent=2))
        elif args.command == "export":
            run.export()
        elif args.command == "down":
            run.down()
        elif args.command == "reset":
            meta = run.metadata()
            run.down()
            archive = run.root.with_name(run.name + "-archive-" + str(time.time_ns()))
            run.root.rename(archive)
            run.up(meta["tier"], meta["mode"], meta["seed"])
        elif args.command == "shell":
            run.dc("exec", "toolbox", "sh", capture=False, timeout=None)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(), file=sys.stderr)
        return 2
    return 0
