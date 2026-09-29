"""Run the real population scripts against an isolated, bundled-version Redis.

Usage: python3 docker/flight-ticket/test_population.py IMAGE ARCH
No cluster, external dataset, exposed port or persistent database is used.
"""

import argparse
import subprocess
import time
import uuid
from pathlib import Path

REDIS_IMAGE = "redis:4.0@sha256:2e03fdd159f4a08d2165ca1c92adde438ae4e3e6b0f74322ce013a78ee81c88d"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=120).stdout.strip()


def check_population(image: str, arch: str):
    name = f"sregym-population-{uuid.uuid4().hex[:12]}"
    server, worker = f"{name}-redis", f"{name}-worker"
    docker("network", "create", "--internal", name)
    try:
        docker(
            "run",
            "-d",
            "--platform",
            f"linux/{arch}",
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
            raise RuntimeError("Redis 4 did not become ready")
        fixture = Path(__file__).with_name("population_fixture.py").read_text()
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--platform",
                f"linux/{arch}",
                "--name",
                worker,
                "--network",
                name,
                "--cpus",
                "1",
                "--memory",
                "768m",
                "-e",
                "REDIS_HOST=redis",
                "-e",
                "REDIS_PORT=6379",
                "-e",
                "REDIS_DB=1",
                "-e",
                "OPENBLAS_NUM_THREADS=1",
                "--entrypoint",
                "python",
                image,
                "-",
            ],
            input=fixture,
            text=True,
            check=True,
            timeout=180,
        )
    finally:
        docker("rm", "-f", "--volumes", worker, server, check=False)
        docker("network", "rm", name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("arch", choices=("amd64", "arm64"))
    args = parser.parse_args()
    check_population(args.image, args.arch)
