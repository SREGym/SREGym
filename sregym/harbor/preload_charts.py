"""Fetch locked Helm dependencies at image-build time, without changing locks."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

import yaml


def build(charts: list[Path]) -> None:
    repositories = set()
    for chart in charts:
        lock = chart / "Chart.lock"
        original = lock.read_bytes()
        dependencies = yaml.safe_load(original).get("dependencies", [])
        for dependency in dependencies:
            repository = dependency.get("repository", "")
            if not repository or repository.startswith(("file://", "oci://")):
                continue
            if repository not in repositories:
                name = "sregym-preload-" + hashlib.sha256(repository.encode()).hexdigest()[:16]
                subprocess.run(["helm", "repo", "add", name, repository], check=True)
                repositories.add(repository)
        # Repository registration already fetched the indexes. Build uses the
        # exact versions in Chart.lock, whereas dependency update resolves anew.
        subprocess.run(["helm", "dependency", "build", "--skip-refresh", str(chart)], check=True)
        if lock.read_bytes() != original:
            raise RuntimeError(f"Helm changed the dependency lock: {lock}")
        result = subprocess.run(["helm", "dependency", "list", str(chart)], check=True, capture_output=True, text=True)
        rows = [[field.strip() for field in line.split("\t")] for line in result.stdout.splitlines()[1:]]
        statuses = [row[-1] for row in rows if len(row) in (3, 4)]
        if len(statuses) != len(dependencies) or any(status not in {"ok", "unpacked"} for status in statuses):
            raise RuntimeError(f"Helm dependencies are not satisfied for {chart}: {result.stdout}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("charts", nargs="+", type=Path)
    args = parser.parse_args()
    build(args.charts)


if __name__ == "__main__":
    main()
