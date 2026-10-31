#!/usr/bin/env python3
"""Restore exact digest references after k3s imports tagged image archives."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

NODES = ["kind-control-plane", "kind-worker", "kind-worker2", "kind-worker3"]
STATUS = "preload-status.json"
SOCKET = "/run/k3s/containerd/containerd.sock"


def image_targets(output: str) -> dict[str, str]:
    result = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2].startswith("sha256:"):
            result[fields[0]] = fields[2]
    return result


def command(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, text=True, capture_output=True, timeout=20)


def restore(manifest: dict, role: str, timeout: float) -> None:
    binary = Path(os.environ.get("K3S_BIN", "/bin"))
    ctr = [str(binary / "ctr"), "--address", SOCKET, "--namespace", "k8s.io"]
    crictl = [str(binary / "crictl"), "--image-endpoint", "unix://" + SOCKET]
    images = {
        source: image for source, image in manifest["images"].items() if role != "server" or image["role"] == "common"
    }
    remaining = set(images)
    deadline = time.monotonic() + timeout
    while remaining and time.monotonic() < deadline:
        listing = command([*ctr, "images", "ls"])
        if listing.returncode:
            time.sleep(1)
            continue
        targets = image_targets(listing.stdout)
        for source in sorted(remaining):
            image = images[source]
            expected = source.rsplit("@", 1)[1]
            tags = image["import_tags"]
            available = next((tag for tag in tags if tag in targets), None)
            if available is None:
                continue
            if targets[available] != expected:
                raise RuntimeError(f"Imported image target changed: {available}: {targets[available]} != {expected}")
            complete = True
            for alias in image["aliases"]:
                if alias in targets and targets[alias] != expected:
                    raise RuntimeError(f"Image alias target changed: {alias}")
                if alias not in targets:
                    tagged = command([*ctr, "images", "tag", "--force", available, alias])
                    if tagged.returncode:
                        raise RuntimeError(f"Cannot restore image alias {alias}: {tagged.stderr}")
                    labeled = command([*ctr, "images", "label", alias, "io.cri-containerd.image=managed"])
                    if labeled.returncode:
                        raise RuntimeError(f"Cannot register image alias {alias}: {labeled.stderr}")
                    targets[alias] = expected
                # Registering metadata emits an asynchronous CRI image event.
                # Wait for exact-reference lookup before declaring the node ready.
                found = command([*crictl, "inspecti", alias])
                if found.returncode:
                    complete = False
            if complete:
                remaining.remove(source)
                print(f"Verified local image references for {source}", flush=True)
        if remaining:
            time.sleep(1)
    if remaining:
        raise TimeoutError(f"Image references were not ready after {timeout}s: {sorted(remaining)}")


def write_status(path: Path, state: str, error: str | None = None) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"state": state, "error": error}) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def wait(root: Path, nodes: list[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = []
        for name in nodes:
            path = root / name / "k3s" / STATUS
            if not path.exists():
                continue
            status = json.loads(path.read_text())
            if status["state"] == "failed":
                raise RuntimeError(f"{name} image preload failed: {status['error']}")
            if status["state"] == "ready":
                ready.append(name)
        if len(ready) == len(nodes):
            print("Every node verified its local image references", flush=True)
            return
        time.sleep(1)
    raise TimeoutError(f"Image preload status was not ready after {timeout}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("restore")
    load.add_argument("--manifest", type=Path, required=True)
    load.add_argument("--role", choices=("server", "agent"), required=True)
    load.add_argument("--status", type=Path, default=Path("/var/lib/rancher/k3s") / STATUS)
    load.add_argument("--timeout", type=float, default=600)
    check = commands.add_parser("wait")
    check.add_argument("--root", type=Path, default=Path("/var/lib/rancher/k3s/nodes"))
    check.add_argument("--nodes", nargs="+", default=NODES)
    check.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    if args.command == "wait":
        wait(args.root, args.nodes, args.timeout)
        return
    try:
        restore(json.loads(args.manifest.read_text()), args.role, args.timeout)
        write_status(args.status, "ready")
    except Exception as error:
        write_status(args.status, "failed", f"{type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    main()
