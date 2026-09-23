"""Host-only lifecycle and outcome evaluation for the native platform.

The operator receives operational hosts and source, never this runner, its
private traffic acknowledgments, source manifests, or evaluator state.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .grading import data_checks
from .topology import SERVICES, TIERS, cache_job, job

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def docker(*args, check=True, timeout=600, input=None):
    return subprocess.run(
        (["sudo", "-n"] if os.geteuid() else []) + ["docker", *map(str, args)],
        input=input,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def api(url, method="GET", data=None, headers=None, raw=False, timeout=10):
    payload = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=payload, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read()
        return content if raw else json.loads(content) if content else None


class Run:
    def __init__(self, name):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,35}", name):
            raise ValueError("invalid run identifier")
        self.name = name
        self.project = "sregym-platform-" + name
        self.root = REPO / "results" / "roblox-platform" / name
        self.compose = self.root / "compose.json"

    def dc(self, *args, **kwargs):
        return docker("compose", "-p", self.project, "-f", self.compose, *args, **kwargs)

    def cid(self, name):
        return self.dc("ps", "-q", name).stdout.strip()

    def ip(self, name):
        info = json.loads(docker("inspect", self.cid(name)).stdout)[0]
        return info["NetworkSettings"]["Networks"][self.project + "_ops"]["IPAddress"]

    def url(self, name, port):
        return f"http://{self.ip(name)}:{port}"

    def metadata(self):
        return json.loads((self.root / "run.json").read_text())

    def consul(self, path, method="GET", data=None, **kwargs):
        return api(self.url("consul-1", 8500) + "/v1/" + path, method, data, **kwargs)

    def nomad(self, path, method="GET", data=None):
        return api(self.url("nomad-1", 4646) + "/v1/" + path, method, data)

    def exec(self, node, *args, **kwargs):
        return self.dc("exec", "-T", node, *args, **kwargs)

    def sql(self, shard, sql):
        return self.exec(
            f"database-{shard}", "psql", "-U", "postgres", "-d", "platform", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql
        ).stdout.strip()

    def up(self, tier="development", build=True, scenario="rollout"):
        if self.compose.exists():
            raise ValueError("run already exists; use a fresh identifier")
        if scenario not in ("rollout", "latent-leader", "recovery-tail"):
            raise ValueError("unknown incident scenario")
        latent = scenario != "rollout"
        recovery = scenario == "recovery-tail"
        if latent and tier not in ("expanded", "fleet"):
            raise ValueError("the latent incident scenarios require expanded or fleet tier")
        spec = dict(TIERS[tier])
        if latent:
            if not (HERE / "bin" / "storage-fixture").exists() or not (HERE / "bin" / "bbolt").exists():
                raise ValueError("build the storage fixture and bbolt CLI before starting latent-leader")
            spec.update(
                routing_tenants=128 if tier == "expanded" else 256,
                routing_replicas=4 if tier == "expanded" else 8,
                placement_replicas=14 if tier == "expanded" else 28,
                placement_catalog_writers=7 if tier == "expanded" else 14,
                placement_interval=0.05 if tier == "expanded" else 0.1,
                workflow_slo_seconds=1.0,
                consul_write_bps=20 * 1024 * 1024,
            )
            if recovery:
                spec["cache_jobs"] = 24 if tier == "expanded" else 96
                spec["cache_controller"] = True
        self.root.mkdir(parents=True, exist_ok=True)
        operations = self.root / "operations"
        operations.mkdir()
        key = self.root / "operator-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        briefing = (HERE / "briefing.md").read_text()
        if latent:
            briefing += "\nPlayer workflows have a one-second latency target.\n"
        (self.root / "briefing.md").write_text(briefing)
        services, volumes = {}, {}
        consul_names = [f"consul-{i}" for i in range(1, 4)]
        workers = [f"worker-{i}" for i in range(1, spec["workers"] + 1)]
        for name in consul_names + ["nomad-1", "vault-1"] + workers:
            role = "worker" if name.startswith("worker") else name.split("-")[0]
            bootstrap = self.root / "bootstrap" / name
            bootstrap.mkdir(parents=True)
            shutil.copyfile(str(key) + ".pub", bootstrap / "authorized_keys")
            configs = {}
            if role in ("worker", "consul"):
                configs["consul"] = {
                    "node_name": name,
                    "data_dir": "/state/consul",
                    "client_addr": "0.0.0.0",
                    "bind_addr": '{{ GetInterfaceIP "eth0" }}',
                    "retry_join": consul_names,
                    "disable_update_check": True,
                    "enable_debug": True,
                    "log_level": "info",
                    "use_streaming_backend": True,
                    "rpc": {"enable_streaming": True},
                    "telemetry": {"prometheus_retention_time": "60s", "disable_hostname": True},
                    "limits": {"http_max_conns_per_client": 10000},
                }
                if role == "consul":
                    configs["consul"].update(server=True, bootstrap_expect=3)
            if role in ("worker", "nomad"):
                configs["nomad"] = {
                    "name": name,
                    "data_dir": "/state/nomad",
                    "bind_addr": "0.0.0.0",
                    "advertise": {p: '{{ GetInterfaceIP "eth0" }}' for p in ("http", "rpc", "serf")},
                    "consul": {
                        "address": "127.0.0.1:8500" if role == "worker" else "consul-1:8500",
                        "auto_advertise": role == "worker",
                    },
                }
                if role == "worker":
                    configs["nomad"].update(
                        client={
                            "enabled": True,
                            "servers": ["nomad-1:4647"],
                            "network_interface": "eth0",
                            "cpu_total_compute": 10000,
                            "memory_total_mb": 8192 if tier == "fleet" else 6144,
                        },
                        plugin=[{"docker": {"config": {
                            "allow_privileged": False,
                            "volumes": {"enabled": recovery},
                        }}}],
                    )
                else:
                    configs["nomad"]["server"] = {"enabled": True, "bootstrap_expect": 1}
            if role == "vault":
                configs["vault"] = {
                    "storage": {"consul": {"address": "consul-1:8500", "path": "vault/"}},
                    "listener": {"tcp": {"address": "0.0.0.0:8200", "tls_disable": True}},
                    "disable_mlock": True,
                    "api_addr": "http://vault-1:8200",
                    "ui": True,
                }
            for component, config in configs.items():
                (bootstrap / (component + ".json")).write_text(json.dumps(config, indent=2))
            mounts = [f"{bootstrap}:/bootstrap:ro"]
            for suffix, target in (("state", "/state"), ("config", "/etc/platform"), ("logs", "/var/log/platform")):
                volumes[name + "-" + suffix] = {}
                mounts.append(f"{name}-{suffix}:{target}")
            if role == "worker":
                volumes[name + "-docker"] = {}
                mounts.append(f"{name}-docker:/var/lib/docker")
            services[name] = {
                "image": "sregym-platform:node",
                "environment": {"ROLE": role},
                "hostname": name,
                "networks": ["ops"],
                "volumes": mounts,
                "mem_limit": "16g" if latent and role == "consul"
                else "8g" if role in ("worker", "consul") else "2g",
            }
            services[name]["ulimits"] = {"nofile": {"soft": 65536, "hard": 65536}}
            if role == "worker":
                services[name]["privileged"] = True
                services[name]["cgroup"] = "private"
        for i in range(2):
            name = f"database-{i}"
            volumes[name] = {}
            services[name] = {
                "image": "postgres:16.10-bookworm",
                "networks": ["ops"],
                "mem_limit": "1g",
                "environment": {"POSTGRES_PASSWORD": "lab-only", "POSTGRES_DB": "platform"},
                "volumes": [f"{name}:/var/lib/postgresql/data"],
            }
        for name in ([] if recovery else [f"cache-{i}" for i in range(4)]) + ["queue"]:
            volumes[name] = {}
            services[name] = {
                "image": "redis:7.2.10-bookworm",
                "networks": ["ops"],
                "mem_limit": "512m",
                "command": ["redis-server", "--appendonly", "yes"],
                "volumes": [f"{name}:/data"],
            }
        services["toolbox"] = {
            "image": "sregym-platform:toolbox",
            "networks": ["ops"],
            "mem_limit": "2g",
            "volumes": [
                f"{operations}:/workspace/operations:ro",
                f"{self.root / 'briefing.md'}:/workspace/briefing.md:ro",
            ],
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }
        self.compose.write_text(
            json.dumps({"services": services, "volumes": volumes, "networks": {"ops": {"internal": True}}}, indent=2)
        )
        meta = {
            "tier": tier,
            "scenario": scenario,
            "spec": spec,
            "created_at": time.time(),
            "project": self.project,
            "source_sha256": {
                str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in HERE.rglob("*")
                if p.is_file() and "bin" not in p.parts and "__pycache__" not in p.parts
            },
        }
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))
        if build:
            for kind in ("node", "app", "toolbox"):
                print("Building", kind, flush=True)
                docker("build", "-f", HERE / ("Dockerfile." + kind), "-t", "sregym-platform:" + kind, HERE)
        print("Starting native control plane and workers", flush=True)
        self.dc("up", "-d")
        self.wait(lambda: bool(self.consul("status/leader")), "Consul quorum")
        self.wait(
            lambda: len([n for n in self.nomad("nodes") if n["Status"] == "ready"]) == len(workers), "Nomad workers"
        )
        self.wait(lambda: self.sql(0, "SELECT 1") == "1" and self.sql(1, "SELECT 1") == "1", "databases")
        self.initialize_data(spec["players"])
        vault_url = self.url("vault-1", 8200)
        self.wait(lambda: not api(vault_url + "/v1/sys/init")["initialized"], "Vault startup")
        credentials = api(vault_url + "/v1/sys/init", "PUT", {"secret_shares": 1, "secret_threshold": 1})
        (operations / "vault-recovery.json").write_text(json.dumps(credentials))
        api(vault_url + "/v1/sys/unseal", "PUT", {"key": credentials["keys"][0]})
        self.wait(lambda: api(vault_url + "/v1/sys/health")["standby"] is False, "Vault active state")
        headers = {"X-Vault-Token": credentials["root_token"]}
        api(vault_url + "/v1/sys/mounts/kv", "POST", {"type": "kv", "options": {"version": "1"}}, headers)
        api(vault_url + "/v1/kv/platform", "POST", {"signing_key": uuid.uuid4().hex}, headers)
        api(
            vault_url + "/v1/sys/policies/acl/platform",
            "PUT",
            {"policy": 'path "kv/platform" { capabilities = ["read"] }'},
            headers,
        )
        token = api(vault_url + "/v1/auth/token/create", "POST", {"policies": ["platform"], "ttl": "168h"}, headers)[
            "auth"
        ]["client_token"]
        self.consul("kv/platform/admission", "PUT", b"100")
        if latent:
            # Prepare the log layout before the application opens long-lived
            # watches. A busy cluster can produce snapshots faster than an
            # offline follower can catch up and be promoted back to a voter.
            peers = self.consul("operator/raft/configuration")["Servers"]
            clean = next(p["Node"] for p in peers if p["Leader"])
            fragmented = [next(p["Node"] for p in peers if not p["Leader"])]
            for node in consul_names:
                docker("cp", HERE / "bin" / "bbolt", self.cid(node) + ":/usr/local/bin/bbolt")
            for node in fragmented:
                print("Preparing historical Raft log layout on", node, flush=True)
                self.prepare_storage(node, 4096)
                self.wait(
                    lambda: len(self.consul("operator/raft/configuration")["Servers"]) == 3
                    and all(p["Voter"] for p in self.consul("operator/raft/configuration")["Servers"]),
                    "Consul voter rejoin", attempts=120,
                )
        image = self.root / "application-image.tar"
        docker("save", "-o", image, "sregym-platform:app")
        for worker in workers:
            print("Loading application image on", worker, flush=True)
            docker("cp", image, self.cid(worker) + ":/tmp/application-image.tar")
            self.exec(worker, "docker", "load", "-i", "/tmp/application-image.tar")
            self.exec(worker, "rm", "/tmp/application-image.tar")
        image.unlink()
        if recovery:
            redis_image = self.root / "redis-image.tar"
            docker("save", "-o", redis_image, "redis:7.2.10-bookworm")
            for i, worker in enumerate(workers):
                docker("cp", redis_image, self.cid(worker) + ":/tmp/redis-image.tar")
                self.exec(worker, "docker", "load", "-i", "/tmp/redis-image.tar")
                self.exec(worker, "rm", "/tmp/redis-image.tar")
            for i in range(spec["cache_jobs"]):
                worker = workers[i % len(workers)]
                path = f"/state/cache-pools/cache-{i}"
                self.exec(worker, "mkdir", "-p", path)
                self.exec(worker, "chown", "-R", "999:999", path)
            redis_image.unlink()
        environment = {
            "CONSUL_HTTP_ADDR": "http://127.0.0.1:8500",
            "CONSUL_SERVERS": ",".join(self.ip(n) for n in consul_names),
            "NOMAD_ADDR": self.url("nomad-1", 4646),
            "VAULT_ADDR": vault_url,
            "VAULT_TOKEN": token,
            "DATABASE_URLS": ",".join(
                f"postgresql://postgres:lab-only@{self.ip('database-' + str(i))}/platform" for i in range(2)
            ),
            "CACHE_HOSTS": "" if recovery else ",".join(self.ip("cache-" + str(i)) for i in range(4)),
            "CACHE_SERVICE_PREFIX": "cache-" if recovery else "",
            "CACHE_POOL_COUNT": str(spec.get("cache_jobs", 4)),
            "QUEUE_HOST": self.ip("queue"),
            "ROUTE_SERVICES": ",".join(SERVICES),
            "ROUTING_TENANTS": str(spec.get("routing_tenants", spec["tenants"])),
            "ROUTING_MODE": "stream",
            "PLACEMENT_SHARDS": str(spec["placement_replicas"]) if latent else "0",
            "CATALOG_WRITERS": str(spec["placement_catalog_writers"]) if latent else "0",
        }
        jobs = {
            f"cache-{i}": cache_job(f"cache-{i}", workers[i % len(workers)])
            for i in range(spec.get("cache_jobs", 0))
        }
        if recovery:
            jobs["cache-reconciler"] = job(
                "cache-reconciler", count=1, environment=environment,
                command="cache_reconciler.py", cpu=100, memory=128,
            )
        jobs.update({name: job(name, count=spec["replicas"], environment=environment) for name in SERVICES})
        jobs["routing"] = job(
            "routing", count=spec.get("routing_replicas", spec["replicas"]), environment=environment,
            command="routing.py", cpu=500, memory=1536 if latent else 512
        )
        if latent:
            jobs["placement"]["TaskGroups"][0]["Count"] = spec["placement_replicas"]
            jobs["placement"]["TaskGroups"][0]["Constraints"] = []
            jobs["placement"]["TaskGroups"][0]["Tasks"][0]["Env"]["RECONCILE_SECONDS"] = str(spec["placement_interval"])
        (operations / "jobs").mkdir()
        for name, definition in jobs.items():
            (operations / "jobs" / (name + ".json")).write_text(json.dumps({"Job": definition}, indent=2))
            self.nomad("jobs", "POST", {"Job": definition})
        inventory = {name: self.ip(name) for name in services if name != "toolbox"}
        (operations / "inventory.json").write_text(json.dumps(inventory, indent=2))
        (operations / "vault-recovery.json").write_text(json.dumps(credentials))
        (operations / "README.md").write_text(self.operator_notes())
        self.configure_toolbox(key)
        print("Waiting for scheduled application services", flush=True)
        self.wait(
            lambda: all(
                len(self.consul(f"health/service/{name}?passing=true")) >= definition["TaskGroups"][0]["Count"]
                for name, definition in jobs.items()
            ),
            "application health",
            attempts=180,
        )
        self.wait(lambda: self.workflow(0, "baseline-" + uuid.uuid4().hex)["ok"], "end-to-end workflow", attempts=60)
        if latent:
            # Initial subscription snapshots create a real but short bootstrap
            # surge. The pre-incident state must be sustained after that surge,
            # rather than treating the rollout itself as the incident.
            consecutive = 0
            for attempt in range(6):
                baseline = self.grade(30)
                (self.root / f"warmup-{attempt}.json").write_text(json.dumps(baseline, indent=2))
                consecutive = consecutive + 1 if baseline["passed"] else 0
                if consecutive == 2:
                    break
        else:
            baseline = self.grade(15)
        (self.root / "baseline.json").write_text(json.dumps(baseline, indent=2))
        if not baseline["passed"] or (latent and consecutive < 2):
            raise RuntimeError("sustained baseline verification failed; inspect baseline.json")
        if latent:
            # The laboratory disk is faster than the production storage path
            # exposed in the postmortem. Bound real block writes equally on all
            # servers; only a fragmented Raft log should amplify small appends.
            self.set_consul_io_cap(spec["consul_write_bps"])
            prepared = self.grade(30)
            (self.root / "prepared-baseline.json").write_text(json.dumps(prepared, indent=2))
            if not prepared["passed"]:
                raise RuntimeError("latent-leader preparation damaged the baseline; inspect prepared-baseline.json")
            meta["clean_leader"] = clean
            meta["fragmented_followers"] = fragmented
        meta["ready_at"] = time.time()
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))
        print(
            json.dumps(
                {
                    "ready": True,
                    "run": self.name,
                    "nomad_allocations": len(self.nomad("allocations")),
                    "containers": len(services),
                }
            ),
            flush=True,
        )

    @staticmethod
    def wait(check, label, attempts=90):
        last = None
        for _ in range(attempts):
            try:
                if check():
                    return
            except Exception as exc:
                last = exc
            time.sleep(2)
        raise RuntimeError(f"{label} not ready: {last}")

    def set_consul_io_cap(self, bytes_per_second):
        """Bound actual block writes on every Consul server through cgroup v2."""
        rate = "max" if bytes_per_second is None else str(bytes_per_second)
        for node in ("consul-1", "consul-2", "consul-3"):
            info = json.loads(docker("inspect", self.cid(node)).stdout)[0]
            pid = info["State"]["Pid"]
            cgroup = next(
                line.split("::", 1)[1] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                if line.startswith("0::")
            )
            path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
            devices = []
            for line in (path / "io.stat").read_text().splitlines():
                fields = dict(item.split("=", 1) for item in line.split()[1:] if "=" in item)
                devices.append((int(fields.get("wbytes", 0)), line.split()[0]))
            if not devices:
                raise RuntimeError(f"no cgroup block device found for {node}")
            device = max(devices)[1]
            subprocess.run(
                (["sudo", "-n"] if os.geteuid() else []) + ["tee", str(path / "io.max")],
                input=f"{device} wbps={rate}\n", text=True, capture_output=True, check=True,
            )

    def raft_last_index(self, node):
        with urllib.request.urlopen(self.url(node, 8500) + "/v1/agent/metrics?format=prometheus", timeout=5) as response:
            metrics = response.read().decode()
        match = re.search(r"^consul_raft_last_index (\d+)$", metrics, re.MULTILINE)
        if not match:
            raise RuntimeError(f"raft last index unavailable on {node}")
        return int(match.group(1))

    def pause_placement(self):
        paused = []
        try:
            for worker in range(1, self.metadata()["spec"]["workers"] + 1):
                node = f"worker-{worker}"
                rows = self.exec(node, "docker", "ps", "--format", "{{.ID}} {{.Names}}").stdout.splitlines()
                ids = []
                for row in rows:
                    fields = row.split()
                    if len(fields) == 2 and fields[1].startswith("placement-"):
                        ids.append(fields[0])
                if ids:
                    self.exec(node, "docker", "pause", *ids)
                    paused.append((node, ids))
            if sum(len(ids) for _, ids in paused) != self.metadata()["spec"]["placement_replicas"]:
                raise RuntimeError("cannot quiesce all placement writers")
        except Exception:
            for node, ids in paused:
                self.exec(node, "docker", "unpause", *ids, check=False)
            raise
        return paused

    def initialize_data(self, players):
        schema = """
        CREATE TABLE players (id integer PRIMARY KEY, name text NOT NULL, coins integer NOT NULL CHECK(coins>=0));
        CREATE TABLE experiences (id integer PRIMARY KEY, document jsonb NOT NULL);
        CREATE TABLE sessions (request_id text PRIMARY KEY, player integer REFERENCES players(id));
        CREATE TABLE purchases (request_id text PRIMARY KEY, player integer REFERENCES players(id), item text NOT NULL);
        CREATE TABLE outbox (request_id text PRIMARY KEY REFERENCES purchases(request_id), player integer NOT NULL,
          published boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now());
        CREATE TABLE receipts (request_id text PRIMARY KEY REFERENCES purchases(request_id), player integer NOT NULL);
        """
        for shard in range(2):
            self.sql(
                shard,
                schema
                + f"INSERT INTO players SELECT n, 'player-'||n, 100000 FROM generate_series(0,{players - 1}) n WHERE n%2={shard};"
                + "INSERT INTO experiences SELECT n, jsonb_build_object('id',n,'name','experience-'||n,'version',1) FROM generate_series(0,31) n;",
            )

    def operator_notes(self):
        return """# Platform operations

`inventory.json` lists management addresses. SSH as root to worker and
control-plane hosts. Workers run isolated Docker daemons managed by Nomad;
their Docker socket controls only their own application allocations.

Nomad jobs and application source describe ownership and dependencies. Use
`nomad job status`, `nomad alloc status`, `nomad alloc logs`, and `nomad alloc exec`
for deployed services. Job specifications in jobs/ can be copied and submitted
with the Nomad HTTP API. Inspect allocation events before changing deployments.

Host configuration lives in /etc/platform, state in /state, and daemon logs in
/var/log/platform. Local Consul metrics and native pprof endpoints are enabled.
These hosts use a minimal init system: launch a daemon with its configuration
file after stopping its old process when a restart is necessary. Keep SSH alive.

The application has two database shards, sharded cache pools, and a durable queue.
The cache-reconciler manages requested pool redeployments through Nomad; inspect
its allocation logs and metrics when cache rollout stalls.
Player identities are immutable. Each purchase costs one coin and creates one
inventory item and one asynchronous receipt. Request IDs make retries idempotent.
Sessions, purchases, and receipts must agree about the player. Processing workers
recover unacknowledged queue messages. Queue depth and unpublished outbox records
help distinguish accepted work from completed work.

Traffic admission is configured in Consul KV at platform/admission (0–100).
Healthy operation serves all cohorts. This setting does not stop internal work.
Vault recovery material is in vault-recovery.json; application tokens have only
read access to their own key. Changes to shared storage can affect secret access.
"""

    def configure_toolbox(self, key, node=None):
        node = node or self.cid("toolbox")
        docker("exec", node, "mkdir", "-p", "/home/agent/.ssh")
        docker(
            "exec",
            "-i",
            node,
            "sh",
            "-c",
            "rm -f /home/agent/.ssh/id_ed25519; umask 077; cat > /home/agent/.ssh/id_ed25519",
            input=key.read_text(),
        )
        config = self.root / "ssh-config"
        config.write_text("Host *\n  StrictHostKeyChecking accept-new\n  User root\n")
        docker(
            "exec",
            "-i",
            node,
            "sh",
            "-c",
            "rm -f /home/agent/.ssh/config; umask 077; cat > /home/agent/.ssh/config",
            input=config.read_text(),
        )
        env = self.root / "operator-env"
        env.write_text(
            f"export NOMAD_ADDR={self.url('nomad-1', 4646)}\nexport CONSUL_HTTP_ADDR={self.url('consul-1', 8500)}\nexport VAULT_ADDR={self.url('vault-1', 8200)}\n"
        )
        docker(
            "exec",
            "-i",
            node,
            "sh",
            "-c",
            "rm -f /home/agent/.profile; cat > /home/agent/.profile",
            input=env.read_text(),
        )

    def workflow(self, player, request_id):
        started = time.time()
        try:
            rows = self.consul("health/service/edge?passing=true")
            row = random.choice(rows)
            url = f"http://{row['Service']['Address'] or row['Node']['Address']}:{row['Service']['Port']}/workflow"
            result = api(url, "POST", {"player": player, "request_id": request_id}, timeout=20)
            valid = (
                result["identity"]["player"] == player
                and result["identity"]["name"] == f"player-{player}"
                and result["session"] == {"request_id": request_id, "player": player}
                and result["purchase"] == {"request_id": request_id, "player": player, "charged": 1}
                and [request_id, "experience-pass"] in result["inventory"]["items"]
            )
            observation = {
                "time": started,
                "elapsed": time.time() - started,
                "player": player,
                "request_id": request_id,
                "ok": valid,
                "incorrect": not valid,
            }
        except Exception as exc:
            observation = {
                "time": started,
                "elapsed": time.time() - started,
                "player": player,
                "request_id": request_id,
                "ok": False,
                "incorrect": False,
                "error": str(exc),
            }

        # Preserve every observed response independently of rotating grade files.
        # A single append is atomic across workload threads/processes on this host.
        record = (json.dumps(observation) + "\n").encode()
        fd = os.open(self.root / "observations.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            if os.write(fd, record) != len(record):
                raise OSError("incomplete evaluator observation write")
        finally:
            os.close(fd)
        return observation

    def traffic(self, duration=0):
        spec = self.metadata()["spec"]
        started, counter = time.monotonic(), 0
        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool,
            (self.root / "traffic.jsonl").open("a", buffering=1) as log,
        ):
            pending = set()
            while not duration or time.monotonic() - started < duration:
                tick = time.monotonic()
                for future in list(pending):
                    if future.done():
                        log.write(json.dumps(future.result()) + "\n")
                        pending.remove(future)
                if len(pending) < 64:
                    for _ in range(spec["rps"]):
                        pending.add(
                            pool.submit(self.workflow, counter % spec["players"], "traffic-" + uuid.uuid4().hex)
                        )
                        counter += 1
                time.sleep(max(0, 1 - (time.monotonic() - tick)))
            for future in concurrent.futures.as_completed(pending):
                log.write(json.dumps(future.result()) + "\n")

    def start_traffic(self):
        pidfile = self.root / "traffic.pid"
        if self.traffic_running():
            raise ValueError("traffic process already exists")
        with (self.root / "traffic-process.log").open("a") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "sregym.postmortems.roblox_platform", "--run", self.name, "traffic"],
                cwd=REPO,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        pidfile.write_text(str(process.pid))
        return {"pid": process.pid}

    def traffic_running(self):
        pidfile = self.root / "traffic.pid"
        if not pidfile.exists():
            return False
        try:
            pid = int(pidfile.read_text())
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError):
            return False
        return b"sregym.postmortems.roblox_platform" in cmd and self.name.encode() in cmd.split(b"\0")

    def stop_traffic(self):
        pidfile = self.root / "traffic.pid"
        if pidfile.exists():
            if self.traffic_running():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            pidfile.unlink()

    def inject(self, tenants=512, placement_interval=0.05):
        """Roll out workload changes against native Consul; no synthetic delays.

        This is an experimental trigger until measurements establish the native
        failure and its causal ablations on each tier. It does not inject a
        fabricated BoltDB fault or claim that workload alone guarantees one.
        """
        meta = self.metadata()
        if "injected_at" in meta:
            raise ValueError("already injected")
        if meta.get("scenario") in ("latent-leader", "recovery-tail"):
            return self.inject_latent(meta)
        snapshot = self.consul("snapshot", raw=True)
        (self.root / "pre-rollout.snap").write_bytes(snapshot)
        for name, changes in (
            ("routing", {"ROUTING_TENANTS": str(tenants)}),
            ("placement", {"RECONCILE_SECONDS": str(placement_interval)}),
        ):
            definition = self.nomad("job/" + name)
            task = definition["TaskGroups"][0]["Tasks"][0]
            task["Env"].update(changes)
            if name == "routing":
                task["Resources"]["MemoryMB"] = 1536
            self.nomad("jobs", "POST", {"Job": definition})
        meta.update(
            injected_at=time.time(),
            trigger={"tenants_per_router": tenants, "placement_interval": placement_interval},
            fidelity="native streaming workload; BoltDB pathology not yet established",
        )
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))
        return meta["trigger"]

    def inject_latent(self, meta):
        """Expose pre-existing Raft file state through an ordinary election.

        The Nomad jobs and final I/O limits remain unchanged. Quiescing the
        placement writers and briefly lifting the common I/O limit lets the
        prepared follower catch up and participate in a native election. Both
        are restored before the operator sees the incident.
        """
        fragmented = set(meta["fragmented_followers"])
        target = meta["fragmented_followers"][0]
        peers = self.consul("operator/raft/configuration")["Servers"]
        clean = next(p["Node"] for p in peers if p["Leader"])
        if clean in fragmented or not all(p["Voter"] for p in peers):
            raise RuntimeError("latent trigger requires a clean leader and three voters")
        meta["clean_leader"] = clean
        paused = []
        elections = []
        elected = None
        traffic_was_running = self.traffic_running()
        if traffic_was_running:
            self.stop_traffic()
        try:
            self.set_consul_io_cap(None)
            paused = self.pause_placement()

            def caught_up():
                leader = next(p["Node"] for p in self.consul("operator/raft/configuration")["Servers"] if p["Leader"])
                return self.raft_last_index(leader) - self.raft_last_index(target) < 100

            self.wait(caught_up, "prepared follower catch-up", attempts=90)
            (self.root / "pre-election.snap").write_bytes(self.consul("snapshot", raw=True))
            for _ in range(16):
                peers = self.consul("operator/raft/configuration")["Servers"]
                current = next(p["Node"] for p in peers if p["Leader"])
                if current in fragmented:
                    elected = current
                    break
                self.wait(caught_up, "prepared follower catch-up before election", attempts=30)
                self.exec(current, "pkill", "-TERM", "-x", "consul")
                try:
                    def follower_elected():
                        nonlocal elected
                        leader = api(self.url(target, 8500) + "/v1/status/leader")
                        elected = next(
                            (name for name in ("consul-1", "consul-2", "consul-3")
                             if name != current and self.ip(name) + ":8300" == leader), None
                        )
                        return elected is not None

                    self.wait(follower_elected, "follower election", attempts=30)
                finally:
                    self.exec(
                        current, "sh", "-c",
                        "nohup consul agent -config-file=/etc/platform/consul.json >> /var/log/platform/consul.log 2>&1 < /dev/null &",
                    )
                elections.append(current + " -> " + elected)
                self.wait(
                    lambda: len(self.consul("operator/raft/configuration")["Servers"]) == 3
                    and all(p["Voter"] for p in self.consul("operator/raft/configuration")["Servers"]),
                    "three voting servers after election", attempts=120,
                )
                if elected in fragmented:
                    break
        finally:
            try:
                self.set_consul_io_cap(meta["spec"]["consul_write_bps"])
            finally:
                for node, ids in paused:
                    self.exec(node, "docker", "unpause", *ids, check=False)
                if traffic_was_running:
                    self.start_traffic()
        if elected not in fragmented:
            raise RuntimeError("prepared follower was not elected after sixteen attempts")
        self.wait(
            lambda: len(self.consul("health/service/placement?passing=true")) >= meta["spec"]["placement_replicas"],
            "placement writers after election", attempts=120,
        )
        cache_fault = meta.get("scenario") == "recovery-tail"
        if cache_fault:
            initial_allocations, cache_epoch = self.arm_cache_rebootstrap(meta["spec"])
            meta["cache_initial_allocations"] = initial_allocations
            meta["cache_redeploy_epoch"] = cache_epoch
        meta.update(
            injected_at=time.time(),
            trigger={
                "elections": elections, "nomad_jobs_changed": False,
                **({"cache_redeploy_requested": cache_epoch} if cache_fault else {}),
            },
            fidelity="native Consul leader election over fragmented Raft log with equal bounded disk throughput",
        )
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))
        grades = []
        for index in range(2):
            result = self.grade(30)
            (self.root / f"fault-grade-{index}.json").write_text(json.dumps(result, indent=2))
            grades.append(result)
        if cache_fault and not all(
            self.consul(f"health/service/cache-{i}?passing=true")
            for i in range(meta["spec"]["cache_jobs"])
        ):
            raise RuntimeError("cache rebootstrap began before operator entry")
        meta["fault_validated"] = all(g["valid"] and not g["passed"] for g in grades)
        (self.root / "run.json").write_text(json.dumps(meta, indent=2))
        if not meta["fault_validated"]:
            raise RuntimeError("latent incident did not fail two settled grades")
        return meta["trigger"]

    def arm_cache_rebootstrap(self, spec):
        """Prepare a latent worker defect and request an ordinary fleet rollout.

        Running Redis processes keep serving. The cache controller only begins
        replacing pools after Consul writes meet its normal readiness target.
        """
        initial = {}
        for index in range(spec["cache_jobs"]):
            name = f"cache-{index}"
            current = [
                row for row in self.nomad("job/" + name + "/allocations")
                if row["DesiredStatus"] == "run" and row["ClientStatus"] == "running"
            ]
            if len(current) != 1:
                raise RuntimeError(f"expected one running {name} allocation before rebootstrap")
            initial[name] = current[0]["ID"]
        for index in range(0, spec["cache_jobs"], spec["workers"]):
            self.exec("worker-1", "chmod", "-R", "a-w", f"/state/cache-pools/cache-{index}")
        epoch = uuid.uuid4().hex
        self.wait(
            lambda: self.consul("kv/platform/cache/redeploy_epoch", "PUT", epoch.encode()) is True,
            "cache redeployment request", attempts=10,
        )
        return initial, epoch

    def inject_cache_bootstrap_fault(self):
        """Leave one Nomad-ready worker unable to start its assigned cache pools.

        The defect lives in the worker's actual storage permissions. Nomad can
        still place the allocation, but Redis cannot open its append-only store.
        A normal allocation stop exercises the native replacement path.
        """
        spec = self.metadata()["spec"]
        pools = [f"cache-{i}" for i in range(0, spec["cache_jobs"], spec["workers"])]
        old_allocations = {}
        for name in pools:
            allocations = [
                row for row in self.nomad("job/" + name + "/allocations")
                if row["DesiredStatus"] == "run" and row["ClientStatus"] == "running"
            ]
            if len(allocations) != 1:
                raise RuntimeError(f"expected one healthy {name} allocation before fault")
            old_allocations[name] = allocations[0]["ID"]
        for name in pools:
            self.exec("worker-1", "chmod", "-R", "a-w", "/state/cache-pools/" + name)
            self.nomad("allocation/" + old_allocations[name] + "/stop", "POST", {})

        def pool_failed(name):
            if self.consul("health/service/" + name + "?passing=true"):
                return False
            for row in self.nomad("job/" + name + "/allocations"):
                if row["ID"] == old_allocations[name]:
                    continue
                task = self.nomad("allocation/" + row["ID"]).get("TaskStates", {}).get("redis", {})
                if any(
                    event.get("Type") == "Terminated" and "Exit Code: 1" in str(event.get("DisplayMessage", ""))
                    for event in task.get("Events", [])
                ):
                    return True
            return False

        self.wait(lambda: all(pool_failed(name) for name in pools), "cache bootstrap failure", attempts=60)

    def prepare_storage(self, node, mib=256):
        """Offline real page-layout fixture, preserving native Raft buckets.

        This accelerates prior storage churn; it does not recreate historical
        traffic byte-for-byte. Effects must be measured rather than assumed.
        """
        if node not in ("consul-1", "consul-2", "consul-3") or not 16 <= mib <= 4096:
            raise ValueError("known voter and 16..4096 MiB required")
        peers = self.consul("operator/raft/configuration")["Servers"]
        if any(row["Node"] == node and row["Leader"] for row in peers):
            raise ValueError("storage preparation requires a follower")
        binary = HERE / "bin" / "storage-fixture"
        if not binary.exists():
            raise ValueError("build the storage fixture first; see README")
        cid = self.cid(node)
        docker("cp", binary, cid + ":/tmp/storage-fixture")
        docker("cp", HERE / "bin" / "bbolt", cid + ":/usr/local/bin/bbolt")
        self.exec(node, "pkill", "-TERM", "-x", "consul", check=False)
        try:
            self.wait(
                lambda: all(
                    s.startswith("Z")
                    for s in self.exec(node, "ps", "-C", "consul", "-o", "stat=", check=False).stdout.split()
                ),
                "follower stop",
                attempts=30,
            )
            output = self.exec(
                node, "/tmp/storage-fixture", "-path", "/state/consul/raft/raft.db", "-mib", str(mib), timeout=600
            ).stdout
            evidence = json.loads(output)
            (self.root / (node + "-storage-preparation.json")).write_text(json.dumps(evidence, indent=2))
            return evidence
        finally:
            self.exec(node, "rm", "-f", "/tmp/storage-fixture")
            self.exec(
                node,
                "sh",
                "-c",
                "nohup consul agent -config-file=/etc/platform/consul.json >> /var/log/platform/consul.log 2>&1 < /dev/null &",
            )

    def grade(self, duration=30):
        if duration < 10:
            raise ValueError("grade duration must be at least 10 seconds")
        try:
            result = self._grade(duration)
            result["valid"] = True
        except Exception as exc:
            result = {"passed": False, "valid": False, "error": str(exc)}
        (self.root / "grade.json").write_text(json.dumps(result, indent=2))
        return result

    def _grade(self, duration):
        meta = self.metadata()
        spec = meta["spec"]
        must_process = set()
        for shard in range(2):
            must_process.update(self.sql(shard, "SELECT request_id FROM purchases").splitlines())
        started = time.time()
        samples = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            futures = []
            for tick in range(duration):
                for j in range(spec["rps"]):
                    player = (tick * spec["rps"] + j) % spec["players"]
                    futures.append(pool.submit(self.workflow, player, "grade-" + uuid.uuid4().hex))
                time.sleep(max(0, started + tick + 1 - time.time()))
            samples = [future.result() for future in futures]
        # Acknowledged work from earlier in the episode must also survive. The
        # workload log lives only on the host and cannot be rewritten by agents.
        path = self.root / "traffic.jsonl"
        prior = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        observations = self.root / "observations.jsonl"
        if observations.exists():
            prior.extend(json.loads(line) for line in observations.read_text().splitlines())
        # The traffic file and observation journal overlap; request IDs identify
        # retries. Keep a successful acknowledgment even if a later retry failed.
        acknowledgments = list(
            {(row["request_id"], row["player"], row["time"]): row for row in prior + samples if row["ok"]}.values()
        )
        shards = []
        for shard in range(2):
            raw = self.sql(
                shard,
                """SELECT json_build_object(
              'players',(SELECT coalesce(json_agg(json_build_array(id,name,coins)),'[]') FROM players),
              'purchases',(SELECT coalesce(json_agg(json_build_array(request_id,player,item)),'[]') FROM purchases),
              'sessions',(SELECT coalesce(json_agg(json_build_array(request_id,player)),'[]') FROM sessions),
              'receipts',(SELECT coalesce(json_agg(json_build_array(request_id,player)),'[]') FROM receipts),
              'unpublished',(SELECT count(*) FROM outbox WHERE NOT published));""",
            )
            shards.append(json.loads(raw))
        must_process.update(row["request_id"] for row in acknowledgments if row["time"] < time.time() - 5)
        durable = data_checks(
            shards=shards, acknowledgments=acknowledgments, players=spec["players"], must_process=must_process
        )
        try:
            admission = self.consul("kv/platform/admission?raw", raw=True).strip() == b"100"
            peers = self.consul("operator/raft/configuration")["Servers"]
            quorum = len(peers) == 3 and all(p["Voter"] for p in peers)
        except (OSError, urllib.error.URLError):
            admission, quorum = False, False
        try:
            workers_ready = len([n for n in self.nomad("nodes") if n["Status"] == "ready"]) == spec["workers"]
        except (OSError, urllib.error.URLError):
            workers_ready = False
        try:
            expected = {name: spec["replicas"] for name in SERVICES}
            expected["routing"] = spec.get("routing_replicas", spec["replicas"])
            expected.update({f"cache-{i}": 1 for i in range(spec.get("cache_jobs", 0))})
            if spec.get("cache_controller"):
                expected["cache-reconciler"] = 1
            # In the latent incident, placement is partitioned. Its outcome is
            # checked through the live shard endpoints below, not job count.
            if "placement_replicas" in spec:
                expected.pop("placement")
            service_capacity = all(
                self.nomad("job/" + name)["TaskGroups"][0]["Count"] >= count
                and len(self.consul(f"health/service/{name}?passing=true")) >= count
                for name, count in expected.items()
            )
        except (OSError, urllib.error.URLError, KeyError, IndexError, TypeError):
            service_capacity = False
        checks = {
            "full_admission": admission,
            "workflows": sum(row["ok"] for row in samples) >= 0.98 * len(samples),
            "correct_responses": not any(row["incorrect"] for row in prior + samples),
            **durable,
            "consul_quorum": quorum,
            "workers_ready": workers_ready,
            "service_capacity": service_capacity,
        }
        if "placement_replicas" in spec:
            checks["placement_shards_live"] = self.placement_shards_live(spec["placement_replicas"])
        if "workflow_slo_seconds" in spec:
            checks["workflow_latency"] = sum(
                row["ok"] and row["elapsed"] <= spec["workflow_slo_seconds"] for row in samples
            ) >= 0.98 * len(samples)
        if meta.get("cache_redeploy_epoch"):
            try:
                complete = self.consul("kv/platform/cache/redeploy_complete?raw", raw=True).decode().strip()
                initial = meta["cache_initial_allocations"]
                replaced = all(
                    any(
                        row["ID"] != initial[f"cache-{i}"]
                        and row["DesiredStatus"] == "run"
                        and row["ClientStatus"] == "running"
                        for row in self.nomad(f"job/cache-{i}/allocations")
                    )
                    for i in range(spec["cache_jobs"])
                )
                checks["cache_redeployment_complete"] = (
                    complete == meta["cache_redeploy_epoch"] and replaced
                )
            except (OSError, urllib.error.URLError, KeyError, ValueError):
                checks["cache_redeployment_complete"] = False
        result = {
            "passed": all(checks.values()),
            "checks": checks,
            "started_at": started,
            "duration": duration,
            "successful": sum(r["ok"] for r in samples),
            "attempted": len(samples),
            "unpublished": sum(s["unpublished"] for s in shards),
            "acknowledgments_checked": len(acknowledgments),
        }
        (self.root / "grade-probes.json").write_text(json.dumps(samples, indent=2))
        return result

    def placement_shards_live(self, count):
        """Require every placement partition to accept work at its current owner."""
        try:
            for shard in range(count):
                owner = json.loads(self.consul(f"kv/platform/placement/{shard}?raw", raw=True))
                if time.time() - owner["updated_at"] > 20:
                    return False
                result = api("http://" + owner["address"] + "/reserve", "POST", {"player": shard})
                if result["shard"] != shard or result["allocation"] != owner["allocation"]:
                    return False
            return True
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError):
            return False

    def export(self):
        (self.root / "container-logs.txt").write_text(self.dc("logs", "--no-color", check=False).stdout)
        manifest = {}
        for name in json.loads(self.compose.read_text())["services"]:
            cid = self.cid(name)
            if not cid:
                continue
            info = json.loads(docker("inspect", cid).stdout)[0]
            manifest[name] = {"image_id": info["Image"], "status": info["State"]["Status"]}
            if name.startswith("worker"):
                manifest[name]["tasks"] = [
                    json.loads(line)
                    for line in self.exec(name, "docker", "ps", "--format", "json", check=False).stdout.splitlines()
                ]
            if name.startswith(("worker", "consul", "nomad", "vault")):
                target = self.root / "host-logs" / name
                target.mkdir(parents=True, exist_ok=True)
                docker("cp", self.cid(name) + ":/var/log/platform/.", target, check=False)
        (self.root / "image-manifest.json").write_text(json.dumps(manifest, indent=2))

    def down(self):
        self.stop_traffic()
        self.export()
        self.dc("down", "-v", "--remove-orphans")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--tier", choices=TIERS, default="development")
    up.add_argument("--scenario", choices=("rollout", "latent-leader", "recovery-tail"), default="rollout")
    up.add_argument("--no-build", action="store_true")
    traffic = sub.add_parser("traffic")
    traffic.add_argument("--duration", type=int, default=0)
    inject = sub.add_parser("inject")
    inject.add_argument("--tenants", type=int, default=512)
    storage = sub.add_parser("prepare-storage")
    storage.add_argument("--node", required=True, choices=["consul-1", "consul-2", "consul-3"])
    storage.add_argument("--mib", type=int, default=256)
    grade = sub.add_parser("grade")
    grade.add_argument("--duration", type=int, default=30)
    for name in ("down", "export", "shell", "start-traffic", "stop-traffic"):
        sub.add_parser(name)
    args = parser.parse_args()
    run = Run(args.run)
    if args.command == "up":
        run.up(args.tier, not args.no_build, args.scenario)
    elif args.command == "traffic":
        run.traffic(args.duration)
    elif args.command == "inject":
        print(json.dumps(run.inject(args.tenants), indent=2))
    elif args.command == "prepare-storage":
        print(json.dumps(run.prepare_storage(args.node, args.mib), indent=2))
    elif args.command == "grade":
        result = run.grade(args.duration)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["passed"] else 1)
    elif args.command == "shell":
        os.execvp("sudo", ["sudo", "-n", "docker", "exec", "-it", run.cid("toolbox"), "bash", "--login"])
    else:
        result = getattr(run, args.command.replace("-", "_"))()
        if result is not None:
            print(json.dumps(result))
