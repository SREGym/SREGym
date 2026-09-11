#!/usr/bin/env python3
"""Build and run isolated SREGym environments using only host Python and Docker."""

import argparse
import os
import subprocess
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMAGE = "sregym-dind:local"
# Forward values by name: secrets never appear in the Docker command line.
CREDENTIALS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
)


def run_command(args):
    run_id = args.name or f"sregym-{uuid.uuid4().hex[:12]}"
    output = (args.output or REPO / "results" / "dind" / run_id).resolve()
    output.mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "--cgroupns=private",
        "--name",
        run_id,
        "--stop-timeout",
        "45",
        "--cpus",
        str(args.cpus),
        "--memory",
        args.memory,
        "--mount",
        f"type=bind,src={output},dst=/opt/sregym/results",
    ]
    if args.docker_tmpfs_size:
        command.extend(
            [
                "--tmpfs",
                f"/run/sregym-docker-data:rw,size={args.docker_tmpfs_size}",
                "--env",
                f"SREGYM_DOCKER_TMPFS_SIZE={args.docker_tmpfs_size}",
            ]
        )
    for name in CREDENTIALS:
        if name in os.environ:
            command.extend(["--env", name])
    if args.env_file:
        command.extend(["--env-file", str(args.env_file.resolve())])
    command.append(args.image)
    payload = args.command
    if payload[:1] == ["--"]:
        payload = payload[1:]
    command.extend(payload or ["sleep", "infinity"])
    return command


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    build = commands.add_parser("build")
    build.add_argument("--image", default=IMAGE)
    run = commands.add_parser("run")
    run.add_argument("--image", default=IMAGE)
    run.add_argument("--name")
    run.add_argument("--output", type=Path)
    run.add_argument("--cpus", type=float, default=8)
    run.add_argument("--memory", default="16g")
    run.add_argument(
        "--docker-tmpfs-size",
        help="Store private Docker data in tmpfs (e.g. 20g); counts against --memory and changes disk behavior",
    )
    run.add_argument("--env-file", type=Path)
    run.add_argument("command", nargs=argparse.REMAINDER)
    return result


def main():
    args = parser().parse_args()
    if args.action == "build":
        if not (REPO / "SREGym-applications" / "README.md").is_file():
            raise SystemExit("Initialize applications first: git submodule update --init --recursive")
        # BuildKit is required for the Dockerfile-specific ignore file.
        command = [
            "docker",
            "buildx",
            "build",
            "--load",
            "-t",
            args.image,
            "-f",
            str(REPO / "docker/dind/Dockerfile"),
            str(REPO),
        ]
    else:
        command = run_command(args)
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        raise SystemExit("Docker is required on the host; install and start Docker first.") from None


if __name__ == "__main__":
    raise SystemExit(main())
