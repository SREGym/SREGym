"""Run Codex in a disposable operator container and grade the native incident.

The agent receives the symptom-only briefing, normal operations files, SSH and
native CLIs. The runner, grader, private observations and host Docker socket are
not mounted. A separate bridge grants outbound inference access.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from .runner import Run, docker


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


def benchmark(run, *, codex_binary, auth_file, model="gpt-6-astra", timeout=3600):
    if "injected_at" not in run.metadata():
        raise ValueError("inject the incident before starting Codex")
    if run.metadata().get("scenario") in ("latent-leader", "recovery-tail") and not run.metadata().get("fault_validated"):
        raise ValueError("latent incident must fail two valid pre-agent grades")
    binary = Path(codex_binary).expanduser().resolve(strict=True)
    auth = Path(auth_file).expanduser().resolve(strict=True)
    name = run.project + "-agent"
    management = run.project + "-management"
    mounts = [
        (binary, "/usr/local/bin/codex"),
        (run.root / "briefing.md", "/workspace/briefing.md"),
        (run.root / "operations", "/workspace/operations"),
    ]
    companion = binary.with_name("codex-code-mode-host")
    if companion.exists():
        mounts.append((companion, "/usr/local/bin/codex-code-mode-host"))
    command = [
        "create", "--name", name, "--network", run.project + "_ops",
        "--user", "1000:1000", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--memory", "2g",
        "--pids-limit", "256", "--workdir", "/workspace",
        "-e", "HOME=/home/agent",
        "-e", "NOMAD_ADDR=" + run.url("nomad-1", 4646),
        "-e", "CONSUL_HTTP_ADDR=" + run.url("consul-1", 8500),
        "-e", "VAULT_ADDR=" + run.url("vault-1", 8200),
    ]
    for source, target in mounts:
        command.extend(["-v", f"{source}:{target}:ro"])
    command.extend(["sregym-platform:toolbox", "sleep", "infinity"])
    started = time.time()
    timed_out = False
    exit_code = None
    docker("network", "create", "--label", "sregym.run=" + run.name, management)
    try:
        docker(*command)
        docker("network", "connect", management, name)
        docker("start", name)
        run.configure_toolbox(run.root / "operator-key", node=name)
        docker("exec", name, "mkdir", "-p", "/home/agent/.codex")
        auth_command = (["sudo", "-n"] if os.geteuid() else []) + [
            "docker", "exec", "-i", name, "sh", "-c",
            "umask 077; cat > /home/agent/.codex/auth.json",
        ]
        with auth.open("rb") as credentials:
            subprocess.run(auth_command, stdin=credentials, check=True, capture_output=True)
        cli = [
            "exec", "--json", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-c", 'web_search="disabled"',
            "-c", "features.multi_agent=false", "--model", model,
            "Read /workspace/briefing.md and resolve the incident. Use the operational tools "
            "to investigate and repair the running system. Do not stop at a plan. "
            "Verify sustained service, then report the outcome.",
        ]
        invocation = (["sudo", "-n"] if os.geteuid() else []) + ["docker", "exec", name, "codex", *cli]
        print(f"Codex started in {name}; timeout {timeout}s", flush=True)
        with (run.root / "codex.jsonl").open("w") as output, (run.root / "codex.stderr.log").open("w") as errors:
            try:
                result = subprocess.run(invocation, stdout=output, stderr=errors, timeout=timeout)
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
        docker("cp", name + ":/home/agent/.codex/sessions", str(run.root / "codex-sessions"), check=False)
        docker("cp", name + ":/workspace/.", str(run.root / "agent-workspace"), check=False)
    finally:
        # Removing the container destroys the copied authentication and stops
        # children left by a timed-out CLI. Native service state remains intact.
        docker("rm", "-f", name, check=False)
        docker("network", "rm", management, check=False)
    print("Codex exited; grading durable outcomes", flush=True)
    grade = run.grade(30)
    run.export()
    result = {
        "agent": "codex", "requested_model": model,
        "timeout_seconds": timeout, "timed_out": timed_out,
        "exit_code": exit_code, "elapsed_seconds": time.time() - started,
        "grade": grade,
    }
    result.update(trace_metadata(run.root))
    (run.root / "benchmark.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--codex", default="/users/jclark58/.local/bin/codex")
    parser.add_argument("--auth", default="~/.codex/auth.json")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    print(json.dumps(benchmark(
        Run(args.run), codex_binary=args.codex, auth_file=args.auth,
        model=args.model, timeout=args.timeout,
    ), indent=2))


if __name__ == "__main__":
    main()
