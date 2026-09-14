"""Problem base class"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EditableFile:
    """A file the agent is allowed to edit for this problem.

    `workspace_path` is relative to the agent workspace (for example
    `recommendation_server.py`). `pod_path` is the absolute path inside the
    running container that the ConfigMap overlays. `deployment` and
    `configmap_name` tell `make deploy` which live objects to update.
    `host_source` is an optional path to the file copied into the workspace
    when the problem does not vendor a full source tree.
    """

    workspace_path: str
    pod_path: str
    deployment: str
    configmap_name: str
    host_source: str | None = None


_DEPLOY_CLI = Path(__file__).resolve().parents[2] / "agent_workspace" / "deploy.py"

_README_TEMPLATE = """# {service_label}

This checkout contains the source for the affected service in the live cluster.
Edit the files here to apply your fix.

## Deploying

To roll your changes out to the cluster:

    make deploy

To preview without applying:

    make deploy-dry-run

To check rollout status of the affected deployment:

    make status
"""

_MAKEFILE_TEMPLATE = """# Workspace deploy targets.
# `make deploy` updates the cluster to match this checkout.

.PHONY: deploy deploy-dry-run status

deploy:
\tpython3 .deploy/deploy.py

deploy-dry-run:
\tpython3 .deploy/deploy.py --dry-run

status:
\tpython3 .deploy/deploy.py --status
"""


class Problem(ABC):
    run_default_workload = True

    def __init__(self, app, namespace: str | None = None):
        self.app = app
        self.namespace = app.namespace if namespace is None else namespace
        self.fault_injected = False
        self.results = {}
        self.root_cause = None  # root cause of the problem in natural language

        # Optional: attach oracles in subclass
        self.diagnosis_oracle = None
        self.mitigation_oracle = None

        # Code-change problems populate these. Config-only problems leave them
        # empty and the agent-workspace pipeline skips them.
        self.editable_files: list[EditableFile] = []
        self.vendored_source_root: Path | None = None

    def requires_khaos(self) -> bool:
        """Override this method to return True if the problem requires Khaos for fault injection."""
        return False

    def has_workspace(self) -> bool:
        """True iff this problem ships a code workspace for the agent."""
        if not self.editable_files:
            return False
        if self.vendored_source_root is not None:
            return True
        return all(editable.host_source for editable in self.editable_files)

    def build_workspace_manifest(self) -> dict:
        """Return a dict suitable for writing to .deploy/manifest.json."""
        return {
            "problem_id": type(self).__name__,
            "namespace": self.namespace,
            "files": [
                {
                    "workspace_path": editable.workspace_path,
                    "pod_path": editable.pod_path,
                    "deployment": editable.deployment,
                    "configmap_name": editable.configmap_name,
                }
                for editable in self.editable_files
            ],
        }

    def provision_workspace(self, host_workspace: Path) -> Path | None:
        """Materialize the code workspace at `host_workspace`.

        Copies vendored source and/or per-file `host_source` assets, then drops
        in a Makefile, README, and a hidden `.deploy/` dir with the manifest
        and bundled deploy CLI. Returns the host workspace path, or None if
        this problem has no editable code.

        Idempotent: re-running wipes and recreates the workspace so each
        attempt starts from a clean baseline.
        """
        if not self.has_workspace():
            return None

        host_workspace = host_workspace.resolve()
        if host_workspace.exists():
            shutil.rmtree(host_workspace)
        host_workspace.mkdir(parents=True)

        if self.vendored_source_root is not None:
            for item in self.vendored_source_root.iterdir():
                target = host_workspace / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)

        for editable in self.editable_files:
            dest = host_workspace / editable.workspace_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if editable.host_source:
                shutil.copy2(Path(editable.host_source), dest)

        deploy_dir = host_workspace / ".deploy"
        deploy_dir.mkdir()
        (deploy_dir / "manifest.json").write_text(
            json.dumps(self.build_workspace_manifest(), indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(_DEPLOY_CLI, deploy_dir / "deploy.py")
        try:
            (deploy_dir / "deploy.py").chmod(0o755)
        except OSError:
            pass

        service_label = ", ".join(sorted({editable.deployment for editable in self.editable_files})) or "service"
        (host_workspace / "Makefile").write_text(_MAKEFILE_TEMPLATE)
        (host_workspace / "README.md").write_text(_README_TEMPLATE.format(service_label=service_label))
        return host_workspace

    def workspace_hint(self) -> str:
        """Short prompt fragment telling the agent the workspace exists.

        Config-only problems return an empty string.
        """
        if not self.has_workspace():
            return ""
        services = ", ".join(sorted({editable.deployment for editable in self.editable_files}))
        return (
            "\n\n"
            "The source code for the affected service "
            f"({services}) is checked out at /workspace. Edit files there "
            "to fix the bug, then run `make deploy` from /workspace to roll "
            "your changes out. Use `make status` to verify the rollout."
        )

    @classmethod
    def build_structured_root_cause(
        cls,
        *,
        component: str,
        namespace: str,
        description: str,
    ) -> str:
        """Return canonical structured root_cause text for judge-side parsing.

        Format:
        [fault_spec] component=<...>; namespace=<...> || <human-readable-description>
        """
        kv = [("component", component), ("namespace", namespace)]
        meta = "; ".join(f"{k}={str(v).strip()}" for k, v in kv)

        return f"[fault_spec] {meta} || {description.strip()}"

    @abstractmethod
    def inject_fault(self):
        pass

    @abstractmethod
    def recover_fault(self):
        pass
