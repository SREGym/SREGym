"""Assemble the public agent sources without copying the private harness."""

import argparse
import hashlib
import re
from pathlib import Path

PUBLIC_MODULES = (
    "sregym/__init__.py",
    "sregym/paths.py",
    "sregym/service/__init__.py",
    "sregym/service/kubectl.py",
    "sregym/service/helm.py",
    "sregym/service/apps/base.py",
    "sregym/service/apps/helpers.py",
)
PUBLIC_SUFFIXES = {".py", ".yaml", ".txt", ".sh"}
PRIVATE_MODULES = {"llm_backend/judge_bridge.py", "clients/cursor/judge_bridge.py"}
IDENTIFIER = re.compile("sregym", re.IGNORECASE)


def public_sources(root: Path) -> dict[str, bytes]:
    """Return the allowlisted, independently importable public runtime."""
    selected = list(PUBLIC_MODULES)
    for directory in ("clients", "logger", "llm_backend", "docker/agents/install-scripts"):
        selected.extend(
            path.relative_to(root).as_posix()
            for path in (root / directory).rglob("*")
            if path.is_file()
            and path.suffix in PUBLIC_SUFFIXES
            and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts)
        )
    sources = {}
    for name in sorted(set(selected)):
        if name in PRIVATE_MODULES:
            continue
        source = root / name
        if any(
            root.joinpath(*Path(name).parts[:index]).is_symlink() for index in range(1, len(Path(name).parts) + 1)
        ) or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Public runtime source escapes the checkout: {name}")
        target = name.replace("sregym/", "incident_runtime/", 1)
        if target.startswith("docker/agents/"):
            target = target.removeprefix("docker/agents/")
        text = source.read_text(encoding="utf-8")
        text = text.replace("SREGym-applications", "applications")
        text = text.replace("SREGYM_ARTIFACT_ID", "RUN_ARTIFACT_ID")
        text = text.replace("SREGYM_PROBLEM_ID", "PROBLEM_ID")
        text = IDENTIFIER.sub("incident_runtime", text)
        sources[target] = text.encode("utf-8")
    # Agent helpers need application utility paths, not fault-script or baseline locations.
    sources["incident_runtime/paths.py"] = (
        b"from pathlib import Path\n\n"
        b"BASE_DIR = Path(__file__).resolve().parent\n"
        b"BASE_PARENT_DIR = BASE_DIR.parent\n"
        b'TARGET_MICROSERVICES = BASE_PARENT_DIR / "applications"\n'
    )
    return sources


def write_context(root: Path, destination: Path, *, base_image: str) -> str:
    """Write only public files, and return their deterministic content identity."""
    sources = public_sources(root)
    dockerfile = (
        b"ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER 0\n"
        b"RUN rm -rf /opt/sregym\n"
        b"COPY runtime/ /opt/runtime/\n"
        b"ENV PYTHONPATH=/opt/runtime\n"
        b'LABEL io.incident.runtime.root="/opt/runtime"\n'
        b"RUN chmod +x /opt/runtime/install-scripts/*.sh\n"
        b'WORKDIR /logs\nENTRYPOINT ["/bin/bash", "-c"]\n'
    )
    digest = hashlib.sha256(base_image.encode() + dockerfile)
    for name, data in sources.items():
        digest.update(name.encode() + b"\0" + data + b"\0")
        target = destination / "runtime" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (destination / "Dockerfile").write_bytes(dockerfile)
    return digest.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    for name, data in public_sources(args.root).items():
        target = args.destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
