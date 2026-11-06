"""Build, and optionally push, the prebuilt images that generated tasks name.

Tasks generated with ``--task-image-repo`` set ``[environment] docker_image``,
so Harbor providers pull that image instead of building ``environment/Dockerfile``
for every trial. This builds each image from the same context Harbor would use,
the task's ``environment/`` directory:

    python -m sregym.harbor.adapter --task-image-repo ghcr.io/sregym/sregym-harbor-task
    python -m sregym.harbor.build_images datasets/sregym --push
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path


def task_images(dataset_dir: Path) -> list[tuple[Path, str]]:
    """(environment dir, image) for every task in dataset_dir that names a docker_image."""
    images = []
    for config_path in sorted(dataset_dir.glob("*/task.toml")):
        image = tomllib.loads(config_path.read_text()).get("environment", {}).get("docker_image")
        if image:
            images.append((config_path.parent / "environment", image))
    return images


def commands(dataset_dir: Path, *, push: bool) -> list[list[str]]:
    """The docker commands that build (and push) every task image in dataset_dir."""
    result = []
    for environment_dir, image in task_images(dataset_dir):
        result.append(["docker", "build", "--tag", image, str(environment_dir)])
        if push:
            result.append(["docker", "push", image])
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the prebuilt images that generated SREGym tasks name.")
    parser.add_argument("dataset_dir", type=Path, help="Directory of generated tasks")
    parser.add_argument("--push", action="store_true", help="Push each image after building it")
    parser.add_argument("--dry-run", action="store_true", help="Print the docker commands without running them")
    args = parser.parse_args(argv)

    planned = commands(args.dataset_dir, push=args.push)
    if not planned:
        parser.error(f"No task in {args.dataset_dir} names a docker_image; generate tasks with --task-image-repo")
    for command in planned:
        print(" ".join(command), flush=True)
        if not args.dry_run:
            subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
