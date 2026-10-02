"""Run the installed Codex CLI in a disposable, source-blind agent container.

Only briefing, ops client, and the CLI binary are mounted. Authentication is
copied into the disposable container (never into an image or result artifact).
The separate agent container has outbound access for inference. Its operational
network cannot reach PostgreSQL, Redis, or raw Consul, and has no Docker socket.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from .runner import HERE, docker


def trace_metadata(root):
    metadata = {}
    for path in (root / "codex-sessions").rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload", {})
            if record.get("type") == "session_meta":
                metadata.update({key: payload[key] for key in ("cli_version", "model_provider") if key in payload})
            elif record.get("type") == "turn_context" and "model" in payload:
                metadata["resolved_model"] = payload["model"]
    path = root / "codex.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "turn.completed":
                metadata["usage"] = record.get("usage", {})
    return metadata


def benchmark(run, *, codex_binary, auth_file, model=None, timeout=600):
    meta = run.metadata()
    if "injected_at" not in meta:
        raise ValueError("inject an incident before starting the agent")
    binary = Path(codex_binary).expanduser().resolve(strict=True)
    auth = Path(auth_file).expanduser().resolve(strict=True)
    name = run.project + "-agent"
    command = [
        "create",
        "--name",
        name,
        "--network",
        run.project + "_ops",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "2g",
        "--pids-limit",
        "256",
        "--workdir",
        "/workspace",
        "-v",
        f"{binary}:/usr/local/bin/codex:ro",
        "-v",
        f"{HERE / 'ops.py'}:/usr/local/bin/ops:ro",
        "-v",
        f"{run.root / 'briefing.md'}:/workspace/briefing.md:ro",
        run.project + ":toolbox",
        "sleep",
        "infinity",
    ]
    companion = binary.with_name("codex-code-mode-host")
    if companion.exists():
        command[-3:-3] = ["-v", f"{companion}:/usr/local/bin/codex-code-mode-host:ro"]
    docker(*command)
    started = time.time()
    timed_out = False
    exit_code = None
    try:
        docker("network", "connect", run.project + "_management", name)
        docker("start", name)
        docker("exec", name, "mkdir", "-p", "/home/agent/.codex")
        auth_command = (["sudo"] if os.geteuid() else []) + [
            "docker",
            "exec",
            "-i",
            name,
            "sh",
            "-c",
            "umask 077; cat > /home/agent/.codex/auth.json",
        ]
        with auth.open("rb") as credentials:
            subprocess.run(auth_command, stdin=credentials, check=True, capture_output=True)
        cli = [
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            'web_search="disabled"',
            "-c",
            "features.multi_agent=false",
        ]
        if model:
            cli.extend(["--model", model])
        cli.append(
            "Read /workspace/briefing.md and resolve the incident. Use the operational tools to investigate and repair the running system. Do not stop at a plan. Verify sustained service, then report the outcome."
        )
        invocation = (["sudo"] if os.geteuid() else []) + ["docker", "exec", name, "codex", *cli]
        with (run.root / "codex.jsonl").open("w") as output, (run.root / "codex.stderr.log").open("w") as errors:
            try:
                result = subprocess.run(invocation, stdout=output, stderr=errors, timeout=timeout)
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
        docker("cp", name + ":/home/agent/.codex/sessions", str(run.root / "codex-sessions"), check=False)
        docker("cp", name + ":/workspace/.", str(run.root / "agent-workspace"), check=False)
    finally:
        # Removing the container also stops any in-container children after a
        # host-side CLI timeout and destroys the copied authentication file.
        docker("rm", "-f", name, check=False)
        run.export()
    result = {
        "agent": "codex",
        "requested_model": model,
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "elapsed_seconds": time.time() - started,
        "grade": run.grade(),
    }
    result.update(trace_metadata(run.root))
    (run.root / "benchmark.json").write_text(json.dumps(result, indent=2))
    return result
