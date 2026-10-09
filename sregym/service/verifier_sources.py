"""Allowlisted verifier sources for Git checkouts and trusted packaged runtimes.

This module uses only the standard library so the DinD launcher can package
the verifier context before installing any SREGym dependencies.
"""

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

SOURCE_DIRS = ("sregym", "logger", "clients", "llm_backend", "SREGym-applications", "docker/verifier")
VERIFIER_SOURCES = (
    "sregym/conductor/problems/tls_clock_drift.py",
    "sregym/conductor/oracles/tls_clock_drift_mitigation.py",
    "sregym/service/docker_runtime.py",
    "sregym/service/workload_volumes.py",
    "sregym/service/verifier_runtime.py",
    "sregym/service/verifier_sources.py",
    "sregym/service/verifier_state.py",
    "sregym/service/verifier_worker.py",
    "docker/verifier/Dockerfile",
)
MANIFEST_NAME = ".sregym-verifier-sources.json"
REQUIRED_SOURCES = frozenset((*VERIFIER_SOURCES, "pyproject.toml", "uv.lock"))


def _allowed_path(root: Path, name: str) -> Path | None:
    if not isinstance(name, str) or "\\" in name:
        return None
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or name != relative.as_posix()
        or not (
            name in {"pyproject.toml", "uv.lock"} or any(name.startswith(directory + "/") for directory in SOURCE_DIRS)
        )
        or any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in relative.parts)
        or relative.suffix in {".pyc", ".pyo"}
    ):
        return None
    path = root.joinpath(*relative.parts)
    if (
        not path.is_file()
        or any(parent.is_symlink() for parent in [path, *path.parents] if parent != root)
        or not path.resolve().is_relative_to(root.resolve())
    ):
        return None
    return path


def _packaged_sources(root: Path, manifest: Path) -> list[Path]:
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Packaged verifier source manifest must be a regular file")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("version") != 1 or not isinstance(metadata.get("files"), dict):
        raise ValueError("Unsupported packaged verifier source manifest")
    files = metadata["files"]
    if not files.keys() >= REQUIRED_SOURCES:
        raise ValueError("Packaged verifier source manifest is missing required files")
    paths = []
    for name, expected in sorted(files.items()):
        path = _allowed_path(root, name)
        if path is None:
            raise ValueError(f"Invalid packaged verifier source path: {name}")
        if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Packaged verifier source hash mismatch: {name}")
        paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def source_files(root: Path) -> list[Path]:
    """Use Git in worktrees, or a build-produced manifest in Git-less images."""
    manifest = root / MANIFEST_NAME
    if not (root / ".git").exists() and (manifest.exists() or manifest.is_symlink()):
        return _packaged_sources(root, manifest)
    try:
        tracked = (
            subprocess.run(
                ["git", "-C", str(root), "ls-files", "--recurse-submodules", "-z", "--", *SOURCE_DIRS],
                capture_output=True,
                check=True,
                timeout=30,
            )
            .stdout.decode()
            .split("\0")
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "Verifier sources require a Git checkout or a trusted packaged manifest; "
            "build DinD images with python3 docker/dind/run.py build"
        ) from exc
    names = set(tracked) | REQUIRED_SOURCES
    return sorted(
        (path for name in names - {""} if (path := _allowed_path(root, name)) is not None),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def write_source_context(root: Path, destination: Path) -> None:
    """Snapshot allowlisted bytes and their hashes into an empty build context."""
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Verifier build context must be empty")
    files = {}
    for path in source_files(root):
        name = path.relative_to(root).as_posix()
        data = path.read_bytes()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
        files[name] = hashlib.sha256(data).hexdigest()
    if not files.keys() >= REQUIRED_SOURCES:
        raise ValueError("Cannot package verifier sources: required files are missing")
    (destination / MANIFEST_NAME).write_text(
        json.dumps({"version": 1, "files": files}, sort_keys=True) + "\n", encoding="utf-8"
    )
