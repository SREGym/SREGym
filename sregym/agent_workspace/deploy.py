#!/usr/bin/env python3
"""deploy — push workspace edits to the live cluster.

Reads `./.deploy/manifest.json` to find the editable files in this checkout,
diffs each against the live ConfigMap that overlays it, and for any change
updates the ConfigMap, restarts the target deployment, and waits for Ready.

Usage:
    python3 .deploy/deploy.py
    python3 .deploy/deploy.py --dry-run
    python3 .deploy/deploy.py --status
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import time
from pathlib import Path


def _run(cmd: list[str], *, check: bool = True, stdin: str | None = None) -> str:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )
    if check and result.returncode != 0:
        sys.stderr.write(f"[deploy] command failed: {' '.join(cmd)}\nstderr: {result.stderr}\n")
        raise SystemExit(result.returncode)
    return result.stdout


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        sys.stderr.write(f"[deploy] manifest not found: {path}\n")
        raise SystemExit(2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[deploy] manifest malformed: {path}: {exc}\n")
        raise SystemExit(2) from exc
    if not isinstance(data, dict) or "files" not in data or "namespace" not in data:
        sys.stderr.write(f"[deploy] manifest malformed: {path}\n")
        raise SystemExit(2)
    return data


def _read_configmap_key(namespace: str, configmap: str, key: str) -> str | None:
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


def _wait_for_rollout_drain(
    namespace: str,
    deployment: str,
    *,
    timeout_seconds: float = 180,
    poll_seconds: float = 1,
) -> None:
    raw_deployment = _run(
        ["kubectl", "get", "deployment", deployment, "-n", namespace, "-o", "json"]
    )
    deployment_data = json.loads(raw_deployment)
    labels = deployment_data.get("spec", {}).get("selector", {}).get("matchLabels") or {}
    if not labels:
        sys.stderr.write(f"[deploy] deployment/{deployment} has no matchLabels selector\n")
        raise SystemExit(1)
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    deadline = time.monotonic() + timeout_seconds
    while True:
        raw_pods = _run(
            ["kubectl", "get", "pods", "-n", namespace, "-l", selector, "-o", "json"]
        )
        pods = json.loads(raw_pods).get("items") or []
        terminating = [
            pod
            for pod in pods
            if (pod.get("metadata") or {}).get("deletionTimestamp")
        ]
        if pods and not terminating:
            return
        if time.monotonic() >= deadline:
            sys.stderr.write(
                f"[deploy] timed out waiting for old deployment/{deployment} pods to terminate\n"
            )
            raise SystemExit(1)
        time.sleep(poll_seconds)


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
            sys.stderr.write(f"[deploy] workspace file missing: {ws_path}\n")
            return 2
        proposed = ws_path.read_text(encoding="utf-8")
        key = Path(entry["pod_path"]).name
        live = _read_configmap_key(namespace, entry["configmap_name"], key)
        if live == proposed:
            print(f". {entry['workspace_path']}  (unchanged)")
            continue

        if args.dry_run:
            print(f"~ {entry['workspace_path']}  (would patch {entry['configmap_name']})")
            _show_diff(entry["workspace_path"], live, proposed)
            continue

        print(f"~ {entry['workspace_path']}  -> configmap/{entry['configmap_name']} key={key}")
        _patch_configmap_key(namespace, entry["configmap_name"], key, proposed)
        changed_deployments.add(entry["deployment"])

    if args.dry_run:
        print("(dry-run; no rollouts triggered)")
        return 0

    for deployment in sorted(changed_deployments):
        print(f"-> rollout restart deployment/{deployment}")
        _rollout_restart(namespace, deployment)
        print(f"-> waiting for rollout of deployment/{deployment} ...")
        _rollout_status(namespace, deployment)
        _wait_for_rollout_drain(namespace, deployment)
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
    deployments = sorted({entry["deployment"] for entry in manifest["files"]})
    for deployment in deployments:
        out = _run(
            [
                "kubectl",
                "get",
                "deployment",
                deployment,
                "-n",
                namespace,
                "-o",
                "jsonpath={.status.readyReplicas}/{.status.replicas}",
            ],
            check=False,
        )
        print(f"{deployment}: {out or '0/0'} ready")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument("--workspace", default=".", help="workspace root (default: cwd)")
    parser.add_argument(
        "--manifest",
        default=".deploy/manifest.json",
        help="path to manifest.json (relative to workspace, or absolute)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show diffs, don't apply")
    parser.add_argument("--status", action="store_true", help="show pod readiness and exit")
    ns = parser.parse_args(argv)
    if ns.status:
        return cmd_status(ns)
    return cmd_deploy(ns)


if __name__ == "__main__":
    raise SystemExit(main())
