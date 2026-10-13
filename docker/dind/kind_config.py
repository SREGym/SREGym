#!/usr/bin/env python3
"""Write the per-run KIND config used inside the DinD runtime.

Starts from kind/kind-config.yaml and applies the options read by
docker/dind/entrypoint.sh:

- SREGYM_ETCD_TMPFS_SIZE (default 512m; 0 disables): memory-backed etcd.
- SREGYM_EXTRA_CA_CERTS: the entrypoint has added this CA to the container's
  bundle; mount that bundle into every node so containerd trusts it.
- SREGYM_REGISTRY_MIRROR: point every node's containerd at the Docker Hub
  mirror described in /run/sregym-containerd-certs.d.

Some sandboxed hosts forbid lowering oom_score_adj, even for root. Kubernetes
requests -998 for every pod sandbox, so runc fails ("can't get final child's
PID from pipe"). On such hosts containerd is told to clamp the value instead,
as KIND does for rootless clusters.
"""

import os
import sys
from pathlib import Path

import yaml

ETCD_HOST_PATH = "/run/sregym-etcd"
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
CONTAINERD_HOSTS_DIR = "/run/sregym-containerd-certs.d"
CONTAINERD_REGISTRY_PATCH = (
    '[plugins."io.containerd.grpc.v1.cri".registry]\n  config_path = "/etc/containerd/certs.d"\n'
)
CONTAINERD_RESTRICT_OOM_PATCH = '[plugins."io.containerd.grpc.v1.cri"]\n  restrict_oom_score_adj = true\n'


def can_lower_oom_score() -> bool:
    """Whether this process may set a negative oom_score_adj (it exits right after)."""
    try:
        Path("/proc/self/oom_score_adj").write_text("-1")
    except OSError:
        return False
    return True


def _mount(node: dict, host_path: str, container_path: str, *, read_only: bool = False) -> None:
    entry = {"hostPath": host_path, "containerPath": container_path}
    if read_only:
        entry["readOnly"] = True
    node.setdefault("extraMounts", []).append(entry)


def build_config(config: dict, env: dict[str, str], *, lower_oom_score: bool = True) -> dict:
    nodes = config["nodes"]
    if env.get("SREGYM_ETCD_TMPFS_SIZE", "512m") != "0":
        _mount(nodes[0], ETCD_HOST_PATH, "/var/lib/etcd")
    for node in nodes:
        if env.get("SREGYM_EXTRA_CA_CERTS"):
            _mount(node, CA_BUNDLE, CA_BUNDLE, read_only=True)
        if env.get("SREGYM_REGISTRY_MIRROR"):
            _mount(node, CONTAINERD_HOSTS_DIR, "/etc/containerd/certs.d", read_only=True)
    if env.get("SREGYM_REGISTRY_MIRROR"):
        config.setdefault("containerdConfigPatches", []).append(CONTAINERD_REGISTRY_PATCH)
    if not lower_oom_score:
        config.setdefault("containerdConfigPatches", []).append(CONTAINERD_RESTRICT_OOM_PATCH)
    return config


def main(argv: list[str]) -> int:
    source, target = map(Path, argv)
    lower_oom_score = can_lower_oom_score()
    if not lower_oom_score:
        print("Host forbids negative oom_score_adj; KIND pods will use clamped OOM scores", file=sys.stderr)
    config = build_config(yaml.safe_load(source.read_text()), dict(os.environ), lower_oom_score=lower_oom_score)
    target.write_text(yaml.safe_dump(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
