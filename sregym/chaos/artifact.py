"""Script-defined fault artifacts.

An artifact is a directory::

    fault.json    metadata (schema below)
    inject.sh     applies the fault; must be idempotent
    recover.sh    restores the original state (used for cleanup)
    mitigate.sh   reference mitigation a competent SRE could apply
    ...           any files the scripts reference (manifests, patches)

Scripts run with ``bash -euo pipefail`` from the artifact directory, with
``NAMESPACE`` (application namespace), ``ARTIFACT_DIR`` and an admin
``KUBECONFIG``. They are executed by the conductor, never inside an agent
session, so the agent sees only the fault's effect on the cluster.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

REQUIRED_SCRIPTS = ("inject.sh", "recover.sh", "mitigate.sh")
REQUIRED_FIELDS = ("app", "title", "fault_layer", "fault_primitive", "root_cause_class", "incident_class")
SUPPORTED_APPS = ("hotel_reservation", "social_network")
# Leaks the fixer could read from cluster state. Scripts may use these words
# in comments; resource names, labels and annotations may not.
SUSPICIOUS_WORDS = ("chaos", "fault", "inject", "sregym", "benchmark")
# Word starts only: "default" must not match "fault".
SUSPICIOUS = re.compile(r"(?<![a-z])(" + "|".join(SUSPICIOUS_WORDS) + ")", re.I)
# Files that can put content into the cluster (scripts, manifests, app config).
CLUSTER_STATE_SUFFIXES = (".sh", ".py", ".yaml", ".yml", ".json", ".lua", ".conf", ".tmpl", ".cfg", ".ini", ".js")

GENERATED_ROOT = Path(__file__).resolve().parents[1] / "conductor" / "problems" / "generated"


def artifact_roots() -> list[Path]:
    roots = [GENERATED_ROOT]
    for extra in filter(None, os.environ.get("SREGYM_GENERATED_DIRS", "").split(":")):
        roots.append(Path(extra))
    return roots


@dataclass
class FaultArtifact:
    path: Path
    meta: dict

    @property
    def problem_id(self) -> str:
        return f"gen_{self.path.name}"

    @property
    def app(self) -> str:
        return self.meta["app"]

    @classmethod
    def load(cls, path: Path) -> FaultArtifact:
        meta = json.loads((path / "fault.json").read_text())
        return cls(path=path, meta=meta)

    def problems(self) -> list[str]:
        """Return structural problems with the artifact (empty when valid)."""
        out = [f"missing {name}" for name in REQUIRED_SCRIPTS if not (self.path / name).is_file()]
        out += [f"fault.json missing {key}" for key in REQUIRED_FIELDS if not self.meta.get(key)]
        if self.meta.get("app") not in SUPPORTED_APPS:
            out.append(f"unsupported app {self.meta.get('app')!r}")
        return out

    def leak_findings(self) -> list[str]:
        """Lines in non-comment script code that would plant a telltale name in the cluster."""
        findings = []
        for file in sorted(self.path.rglob("*")):
            if not file.is_file() or file.suffix not in CLUSTER_STATE_SUFFIXES:
                continue
            if file.suffix == ".json" and '"apiVersion"' not in file.read_text(errors="ignore"):
                continue  # notes and validation records, not manifests
            try:
                lines = file.read_text().splitlines()
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(lines, 1):
                code = line.split("#", 1)[0] if file.suffix == ".sh" else line
                if SUSPICIOUS.search(code):
                    findings.append(f"{file.name}:{n}: {line.strip()[:160]}")
        return findings

    def workdir(self) -> Path:
        """Writable copy of the artifact, so inject.sh can leave state for recover.sh."""
        root = os.environ.get("SREGYM_FAULT_WORKDIR") or Path(tempfile.gettempdir()) / "sregym-fault-work"
        work = Path(root) / self.path.name
        if not work.is_dir():
            shutil.copytree(self.path, work)
            for path in (work, *work.rglob("*")):
                path.chmod(path.stat().st_mode | 0o700 if path.is_dir() else path.stat().st_mode | 0o600)
        return work

    def run(self, script: str, namespace: str, timeout_s: int = 900) -> subprocess.CompletedProcess:
        work = self.workdir()
        env = dict(os.environ)
        env.update(NAMESPACE=namespace, ARTIFACT_DIR=str(work))
        admin = Path.home() / ".kube" / "config"
        if admin.is_file():
            env["KUBECONFIG"] = str(admin)
        return subprocess.run(
            ["bash", "-euo", "pipefail", str(work / script)],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )


def discover() -> dict[str, FaultArtifact]:
    found: dict[str, FaultArtifact] = {}
    for root in artifact_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if (path / "fault.json").is_file():
                try:
                    artifact = FaultArtifact.load(path)
                except (OSError, json.JSONDecodeError):
                    continue
                found.setdefault(artifact.problem_id, artifact)
    return found
