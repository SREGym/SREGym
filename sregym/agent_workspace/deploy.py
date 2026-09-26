#!/usr/bin/env python3
"""deploy — push workspace edits to the live cluster.

Reads `./.deploy/manifest.yaml` to find the editable files in this checkout,
diffs each against the live ConfigMap that overlays it in the cluster, and
for any change: updates the ConfigMap, triggers a rollout of the target
deployment, and waits for pods to become Ready.

Usage:
    deploy                # deploy all changed files in workspace
    deploy --dry-run      # show what would change, no writes
    deploy --status       # report pod readiness for declared deployments
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], *, check: bool = True, stdin: str | None = None) -> str:
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )
    if check and res.returncode != 0:
        sys.stderr.write(
            f"[deploy] command failed: {' '.join(cmd)}\n"
            f"stderr: {res.stderr}\n"
        )
        raise SystemExit(res.returncode)
    return res.stdout


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        sys.stderr.write(f"[deploy] manifest not found: {path}\n")
        raise SystemExit(2)
    import yaml  # type: ignore

    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or "files" not in data or "namespace" not in data:
        sys.stderr.write(f"[deploy] manifest malformed: {path}\n")
        raise SystemExit(2)
    return data


def _read_configmap_key(namespace: str, configmap: str, key: str) -> str | None:
    """Return the current value of configmap's data[key], or None if not present."""
    out = _run(
        ["kubectl", "get", "configmap", configmap, "-n", namespace, "-o", "json"],
        check=False,
    )
    if not out.strip():
        return None
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return None
    return (obj.get("data") or {}).get(key)


def _patch_configmap_key(namespace: str, configmap: str, key: str, value: str) -> None:
    """Replace configmap's data[key] with `value` via `kubectl patch`."""
    patch = json.dumps({"data": {key: value}})
    _run(
        [
            "kubectl",
            "patch",
            "configmap",
            configmap,
            "-n",
            namespace,
            "--type=merge",
            "-p",
            patch,
        ]
    )


def _rollout_restart(namespace: str, deployment: str) -> None:
    _run(["kubectl", "rollout", "restart", f"deployment/{deployment}", "-n", namespace])


def _rollout_status(namespace: str, deployment: str, timeout: str = "180s") -> None:
    _run(
        [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n",
            namespace,
            f"--timeout={timeout}",
        ]
    )


def _show_diff(workspace_path: str, live: str | None, proposed: str) -> None:
    live_lines = (live or "").splitlines(keepends=True)
    proposed_lines = proposed.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            live_lines,
            proposed_lines,
            fromfile=f"live:{workspace_path}",
            tofile=f"workspace:{workspace_path}",
            n=2,
        )
    )
    if diff:
        sys.stdout.writelines(diff)
    else:
        print(f"(no change in {workspace_path})")


def cmd_deploy(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = workspace / manifest_path
    manifest = _load_manifest(manifest_path)
    namespace = manifest["namespace"]
    files = manifest["files"]

    changed_deployments: set[str] = set()
    for entry in files:
        ws_path = workspace / entry["workspace_path"]
        if not ws_path.exists():
            sys.stderr.write(
                f"[deploy] workspace file missing: {ws_path}\n"
            )
            return 2
        proposed = ws_path.read_text()
        key = Path(entry["pod_path"]).name
        live = _read_configmap_key(namespace, entry["configmap_name"], key)
        if live == proposed:
            print(f"· {entry['workspace_path']}  (unchanged)")
            continue

        if args.dry_run:
            print(f"~ {entry['workspace_path']}  (would patch {entry['configmap_name']})")
            _show_diff(entry["workspace_path"], live, proposed)
            continue

        print(
            f"~ {entry['workspace_path']}  → configmap/{entry['configmap_name']}"
            f" key={key}"
        )
        _patch_configmap_key(namespace, entry["configmap_name"], key, proposed)
        changed_deployments.add(entry["deployment"])

    if args.dry_run:
        print("(dry-run; no rollouts triggered)")
        return 0

    for deployment in sorted(changed_deployments):
        print(f"→ rollout restart deployment/{deployment}")
        _rollout_restart(namespace, deployment)
        print(f"→ waiting for rollout of deployment/{deployment} …")
        _rollout_status(namespace, deployment)
        print(f"  deployment/{deployment} ready")

    if not changed_deployments:
        print("Nothing to deploy.")
    else:
        print(f"Deployed {len(changed_deployments)} deployment(s).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = workspace / manifest_path
    manifest = _load_manifest(manifest_path)
    namespace = manifest["namespace"]
    deployments = sorted({f["deployment"] for f in manifest["files"]})
    for d in deployments:
        out = _run(
            [
                "kubectl",
                "get",
                "deployment",
                d,
                "-n",
                namespace,
                "-o",
                "jsonpath={.status.readyReplicas}/{.status.replicas}",
            ],
            check=False,
        )
        print(f"{d}: {out or '0/0'} ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deploy")
    ap.add_argument("--workspace", default=".", help="workspace root (default: cwd)")
    ap.add_argument(
        "--manifest",
        default=".deploy/manifest.yaml",
        help="path to manifest.yaml (relative to workspace, or absolute)",
    )
    ap.add_argument("--dry-run", action="store_true", help="show diffs, don't apply")
    ap.add_argument("--status", action="store_true", help="show pod readiness and exit")
    ns = ap.parse_args(argv)
    if ns.status:
        return cmd_status(ns)
    return cmd_deploy(ns)


if __name__ == "__main__":
    raise SystemExit(main())
