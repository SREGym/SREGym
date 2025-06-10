#!/usr/bin/env python3
"""
Create a CloudLab experiment *and* stand-up a Kubernetes cluster.

Usage example
-------------
python3 scripts/geni-lib/quick_k8s_experiment.py \
    --site wisconsin --hardware-type c220g5 --nodes 3 --duration 2 \
    --ssh-user saleha --ssh-key ~/cloudlab_decrypted.pem
"""
from __future__ import annotations

import argparse
import datetime
import random
import re
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import geni.portal as portal
import geni.util
from geni.aggregate.cloudlab import Clemson, Utah, Wisconsin

from scripts.geni_lib.cluster_setup import nodes_reachable, setup_cloudlab_cluster

AGG = {"utah": Utah, "clemson": Clemson, "wisconsin": Wisconsin}


def _extract_hosts(li) -> list[str]:
    hosts: list[str] = []
    pat = re.compile(r"]\s+([^\s:]+)")
    for item in li if isinstance(li, (list, tuple)) else [li]:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            hosts.append(item[1])
            continue
        m = pat.search(str(item))
        if m:
            hosts.append(m.group(1))
    return hosts


def wait_ssh(section: dict, timeout: int = 600) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if nodes_reachable(section):
            return
        time.sleep(10)
    raise RuntimeError("nodes never became reachable over SSH")


def main() -> None:
    ap = argparse.ArgumentParser("quick_k8s_experiment")
    ap.add_argument("--slice-name")
    ap.add_argument("--site", choices=AGG, default="wisconsin")
    ap.add_argument("--hardware-type", default="c220g5")
    ap.add_argument("--nodes", type=int, default=3)
    ap.add_argument("--duration", type=int, default=2, help="hours")
    ap.add_argument("--os-type", default="UBUNTU22-64-STD")
    ap.add_argument("--ssh-user", required=True)
    ap.add_argument("--ssh-key", help="private key file (optional, falls back to agent)")
    ap.add_argument("--pod-network-cidr", default="192.168.0.0/16")
    args = ap.parse_args()

    ctx = geni.util.loadContext()
    slice_name = args.slice_name or f"k8s-{random.randint(100_000,999_999)}"
    exp_time = datetime.datetime.now() + datetime.timedelta(hours=args.duration)

    # tiny RSpec
    req = portal.context.makeRequestRSpec()
    pcs = []
    for i in range(args.nodes):
        n = req.RawPC(f"node{i}")
        n.hardware_type = args.hardware_type
        n.disk_image = f"urn:publicid:IDN+emulab.net+image+emulab-ops//" f"{args.os_type}"
        n.routable_control_ip = True
        pcs.append(n)
    req.Link(members=pcs)

    print(f"🛠  Slice {slice_name} → {args.site}")
    ctx.cf.createSlice(ctx, slice_name, exp=exp_time, desc="Quick K8s experiment")
    manifest = AGG[args.site].createsliver(ctx, slice_name, req)

    geni.util.printlogininfo(manifest=manifest)

    hosts = _extract_hosts(geni.util._corelogininfo(manifest))
    cfg = {
        "cloudlab": {"ssh_user": args.ssh_user, "ssh_key": args.ssh_key, "nodes": hosts},
        "pod_network_cidr": args.pod_network_cidr,
    }

    print("⌛  Waiting for SSH …")
    wait_ssh(cfg["cloudlab"])
    print("🚀  Bootstrapping Kubernetes …")
    setup_cloudlab_cluster(cfg)
    print("✅  Cluster ready!")


if __name__ == "__main__":
    main()
