#!/usr/bin/env python3
"""Attribute host memory (RSS/PSS) and CPU to cluster component categories.

Walks /proc, classifies every process by its command line, samples CPU over an
interval, and prints a per-category table plus MemTotal-MemAvailable.

usage: footprint.py [--interval SECONDS] [--json OUT] [--label NAME]
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")

# Ordered: first match wins.  Patterns run against "comm\0args".
CATEGORIES = [
    ("control-plane", r"kube-apiserver|(^|/)etcd( |$|\0)|kube-controller-manager|kube-scheduler|kine"),
    ("kwok", r"(^|/)kwok( |\0|$)|kwok-controller"),
    ("kubelet", r"(^|/)kubelet( |\0|$)"),
    ("container-runtime", r"containerd-shim|(^|/)containerd( |\0|$)|(^|/)runc|(^|/)pause( |\0|$)|/pause$"),
    ("kube-proxy", r"kube-proxy"),
    ("cni", r"calico|bird|felix|confd|kindnet|flannel|cilium"),
    ("cluster-addons", r"coredns|local-path-provisioner|metrics-server"),
    (
        "observability",
        r"prometheus|jaeger|loki|otelcol|opentelemetry|filebeat|logstash|grafana|node_exporter|kube-state-metrics|alertmanager",
    ),
    ("procnode", r"procnode\.py"),
    ("supervisor", r"supervisord|minikube-agent"),
    ("node-init", r"systemd|journald|dbus|(^|/)sleep( |\0|$)|/sbin/init"),
    ("docker-host", r"dockerd|docker-proxy"),
]

APP_HINTS = r"mongod|memcached|consul|hotel|frontend|geo|profile|rate|recommendation|reservation|search|user|java|node|python|redis|mysql|postgres|nginx|envoy|kafka|zookeeper"


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def snapshot():
    procs = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        stat = read(f"/proc/{pid}/stat")
        if stat is None:
            continue
        stat = stat.decode(errors="replace")
        rparen = stat.rfind(")")
        comm = stat[stat.find("(") + 1 : rparen]
        fields = stat[rparen + 2 :].split()
        ppid = int(fields[1])
        ticks = int(fields[11]) + int(fields[12])
        rss_pages = int(fields[21])
        cmd = (read(f"/proc/{pid}/cmdline") or b"").replace(b"\0", b" ").decode(errors="replace").strip()
        procs[int(pid)] = dict(comm=comm, cmd=cmd, ppid=ppid, ticks=ticks, rss=rss_pages * PAGE)
    return procs


def pss(pid):
    data = read(f"/proc/{pid}/smaps_rollup")
    if not data:
        return None
    m = re.search(rb"^Pss:\s+(\d+) kB", data, re.M)
    return int(m.group(1)) * 1024 if m else None


POD_UID = re.compile(r"pod([0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12})")


PROCNODE_CG = re.compile(r"/procnode/([0-9a-f-]{12})-")


def pod_namespace(pid, uid2ns):
    cg = read(f"/proc/{pid}/cgroup")
    if not cg or not uid2ns:
        return None
    text = cg.decode(errors="replace")
    m = POD_UID.search(text)
    if m:
        return uid2ns.get(m.group(1).replace("_", "-"))
    m = PROCNODE_CG.search(text)  # procnode: memory:/procnode/<uid[:12]>-<container>
    if m:
        return uid2ns.get(m.group(1))
    return None


def classify(p, procs, me):
    hay = f"{p['comm']}\0{p['cmd']}"
    for name, pat in CATEGORIES:
        if re.search(pat, hay):
            return name
    # Anything that descends from a containerd-shim is a pod workload.
    cur, seen = p["ppid"], 0
    while cur in procs and seen < 30:
        if "containerd-shim" in procs[cur]["comm"] or "containerd-shim" in procs[cur]["cmd"]:
            return "app"
        cur, seen = procs[cur]["ppid"], seen + 1
    if re.search(APP_HINTS, hay):
        return "app"
    return "other"


def meminfo():
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":")
        out[k] = int(v.split()[0]) * 1024
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--json")
    ap.add_argument("--label", default="")
    ap.add_argument("--top", type=int, default=0, help="also print top-N processes by RSS")
    ap.add_argument(
        "--pods-json", help="kubectl get pods -A -o json output; attributes pod processes to ns:<namespace>"
    )
    a = ap.parse_args()

    me = os.getpid()
    uid2ns = {}
    if a.pods_json:
        for item in json.loads(Path(a.pods_json).read_text())["items"]:
            uid2ns[item["metadata"]["uid"]] = item["metadata"]["namespace"]
            uid2ns[item["metadata"]["uid"][:12]] = item["metadata"]["namespace"]
    s0 = snapshot()
    t0 = time.time()
    time.sleep(a.interval)
    s1 = snapshot()
    dt = time.time() - t0
    mi = meminfo()

    cats = {}
    rows = []
    for pid, p in s1.items():
        if pid == me:
            continue
        # Exclude the measurement harness and the interactive shell tooling.
        if "footprint.py" in p["cmd"] or "claude" in p["cmd"].lower() or "process_api" in p["cmd"]:
            continue
        cat = classify(p, s1, me)
        ns = pod_namespace(pid, uid2ns)
        if ns and cat in (
            "app",
            "other",
            "cni",
            "observability",
            "cluster-addons",
            "kube-proxy",
            "supervisor",
            "node-init",
        ):
            cat = {"kube-system": f"ns:kube-system/{cat}"}.get(ns, f"ns:{ns}")
        cpu = 0.0
        if pid in s0:
            cpu = (p["ticks"] - s0[pid]["ticks"]) / CLK / dt
        ps = pss(pid) or p["rss"]
        c = cats.setdefault(cat, dict(n=0, rss=0, pss=0, cpu=0.0))
        c["n"] += 1
        c["rss"] += p["rss"]
        c["pss"] += ps
        c["cpu"] += cpu
        rows.append((p["rss"], ps, cpu, cat, pid, p["comm"], p["cmd"][:90]))

    total = dict(n=0, rss=0, pss=0, cpu=0.0)
    print(f"== {a.label}  (cpu sampled over {dt:.1f}s)")
    print(f"{'category':<20}{'procs':>6}{'RSS MiB':>10}{'PSS MiB':>10}{'CPU cores':>11}")
    for cat, c in sorted(cats.items(), key=lambda kv: -kv[1]["pss"]):
        print(f"{cat:<20}{c['n']:>6}{c['rss'] / 2**20:>10.0f}{c['pss'] / 2**20:>10.0f}{c['cpu']:>11.3f}")
        for k in total:
            total[k] += c[k]
    print(
        f"{'TOTAL':<20}{total['n']:>6}{total['rss'] / 2**20:>10.0f}{total['pss'] / 2**20:>10.0f}{total['cpu']:>11.3f}"
    )
    used = mi["MemTotal"] - mi["MemAvailable"]
    print(f"host MemTotal-MemAvailable: {used / 2**20:.0f} MiB   (Shmem {mi['Shmem'] / 2**20:.0f} MiB)")
    if a.top:
        print("-- top processes by PSS")
        for r in sorted(rows, key=lambda r: -r[1])[: a.top]:
            print(f"{r[1] / 2**20:8.0f} MiB {r[2]:6.3f} cpu  {r[3]:<18} {r[5]:<16} {r[6]}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(
                dict(
                    label=a.label,
                    interval=dt,
                    categories=cats,
                    total=total,
                    host_used=used,
                    top=[
                        dict(pss=r[1], cpu=r[2], cat=r[3], comm=r[5], cmd=r[6])
                        for r in sorted(rows, key=lambda r: -r[1])[:40]
                    ],
                ),
                f,
                indent=1,
            )


if __name__ == "__main__":
    main()
