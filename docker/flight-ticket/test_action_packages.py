"""Test the deployer's shipped ZIPs, not newly assembled test-only packages.

Every action initializes in a fresh instance of the exact declared runtime.
Real read/write actions also exercise the bundled Redis 4 server. No Docker
socket, privileged container, external dataset or real cluster is needed.
"""

import argparse
import json
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

REDIS_IMAGE = "redis:4.0@sha256:2e03fdd159f4a08d2165ca1c92adde438ae4e3e6b0f74322ce013a78ee81c88d"


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError(f"Docker command failed: {args}\n{result.stderr}")
    return result.stdout.strip()


def check_packages(image, arch):
    name = "sregym-action-packages-" + uuid.uuid4().hex[:12]
    deployer, server, worker = name + "-deployer", name + "-redis", name + "-worker"
    docker("network", "create", "--internal", name)
    try:
        docker("create", "--platform", "linux/" + arch, "--name", deployer, image)
        environment = json.loads(docker("inspect", deployer, "--format", "{{json .Config.Env}}"))
        runtime = next(
            (value.split("=", 1)[1] for value in environment if value.startswith("PYTHON_RUNTIME_IMAGE=")), None
        )
        if not runtime:
            raise ValueError("Deployer does not declare PYTHON_RUNTIME_IMAGE")
        with tempfile.TemporaryDirectory(prefix="sregym-shipped-actions-") as temporary:
            packages = Path(temporary) / "actions"
            docker("cp", deployer + ":/app/actions", str(packages))
            actions = sorted(path for path in packages.iterdir() if path.is_dir())
            assert len(actions) == 14, "Expected all 14 FlightTicket actions"
            for action in actions:
                with zipfile.ZipFile(action / "function.zip") as archive:
                    assert archive.read("__main__.py") == (action / "__main__.py").read_bytes()
                    assert archive.read("virtualenv/bin/activate_this.py")
                    assert archive.read("virtualenv/lib/python3.6/site-packages/redis/__init__.py")
                    header = archive.read("virtualenv/bin/python")[:20]
                    assert header[:4] == b"\x7fELF"
                    assert int.from_bytes(header[18:20], "little") == {"arm64": 183, "amd64": 62}[arch]
            docker(
                "run",
                "-d",
                "--platform",
                "linux/" + arch,
                "--name",
                server,
                "--network",
                name,
                "--network-alias",
                "redis",
                "--memory",
                "128m",
                REDIS_IMAGE,
            )
            for _ in range(30):
                if docker("exec", server, "redis-cli", "ping", check=False) == "PONG":
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Redis did not become ready")
            docker("exec", server, "redis-cli", "-n", "1", "HSET", "station", "PackagingTest", "1234")
            docker("exec", server, "redis-cli", "-n", "1", "HSET", "money", "packaging-test", "100")
            fixture = Path(__file__).with_name("action_package_fixture.py").resolve()
            for action in actions:
                output = docker(
                    "run",
                    "--rm",
                    "--platform",
                    "linux/" + arch,
                    "--name",
                    worker,
                    "--network",
                    name,
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--ulimit",
                    "nofile=65536:65536",
                    "-v",
                    str(packages) + ":/packages:ro",
                    "-v",
                    str(fixture) + ":/fixture.py:ro",
                    "-e",
                    "ACTION=" + action.name,
                    "-e",
                    "EXPECTED_ARCH=" + arch,
                    "--entrypoint",
                    "python",
                    runtime,
                    "/fixture.py",
                )
                print(output, flush=True)
            assert float(docker("exec", server, "redis-cli", "-n", "1", "HGET", "money", "packaging-test")) == 103
            print(
                json.dumps(
                    {
                        "passed": True,
                        "architecture": arch,
                        "initialized_actions": len(actions),
                        "redis_read_write": True,
                    }
                )
            )
    finally:
        docker("rm", "-f", "--volumes", deployer, worker, server, check=False)
        docker("network", "rm", name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("arch", choices=("amd64", "arm64"))
    args = parser.parse_args()
    check_packages(args.image, args.arch)
