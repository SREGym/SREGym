"""Problem base class"""

import shutil
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class EditableFile:
    """A file the agent is allowed to edit for this problem.

    `workspace_path` is the relative path inside the workspace (e.g.
    "src/product-reviews/database.py"). `pod_path` is the absolute path
    inside the running container that this file is mounted over.
    `deployment` + `configmap_name` tell the deploy script which live
    objects to update and which deployment to roll.
    """

    workspace_path: str
    pod_path: str
    deployment: str
    configmap_name: str


# Path to the deploy CLI we bundle into every workspace's hidden .deploy/ dir.
# Computed relative to this module so the module can be imported from anywhere.
_DEPLOY_CLI = Path(__file__).resolve().parents[2] / "agent_workspace" / "deploy.py"


_README_TEMPLATE = """# {service_label}

This checkout contains the source for the affected service(s) in the live
cluster. Edit files in `src/` to apply your fix.

## Deploying

To roll your changes out to the cluster:

    make deploy

To preview without applying:

    make deploy-dry-run

To check rollout status of the affected deployment(s):

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
    def __init__(self, app, namespace: str):
        self.app = app
        self.namespace = namespace
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

    def build_workspace_manifest(self) -> dict:
        """Return a dict suitable for writing to .deploy/manifest.yaml.

        The deploy CLI reads this to decide which workspace files map to
        which live ConfigMap + deployment, and where to roll out.
        """
        return {
            "problem_id": type(self).__name__,
            "namespace": self.namespace,
            "files": [asdict(f) for f in self.editable_files],
        }

    def has_workspace(self) -> bool:
        """True iff this problem ships a code workspace for the agent."""
        return bool(self.editable_files) and self.vendored_source_root is not None

    def provision_workspace(self, host_workspace: Path) -> Path | None:
        """Materialize the code workspace at `host_workspace`.

        Copies the vendored service source tree into `host_workspace`,
        then drops in a Makefile, README, and a hidden `.deploy/` dir
        with the manifest + bundled deploy CLI. Returns the host
        workspace path, or None if this problem has no editable code.

        Idempotent: re-running wipes and re-creates the workspace, which
        is what we want — agent runs from a clean baseline each iteration.
        """
        import yaml  # type: ignore

        if not self.has_workspace():
            return None

        host_workspace = host_workspace.resolve()
        if host_workspace.exists():
            shutil.rmtree(host_workspace)
        host_workspace.mkdir(parents=True)

        # Copy vendored source tree under the workspace root.
        src_dir = self.vendored_source_root  # type: ignore[union-attr]
        for item in src_dir.iterdir():  # type: ignore[union-attr]
            target = host_workspace / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

        # Hidden .deploy/ dir holds the manifest + bundled CLI; agents
        # generally won't touch this and `ls` won't show it by default.
        deploy_dir = host_workspace / ".deploy"
        deploy_dir.mkdir()
        (deploy_dir / "manifest.yaml").write_text(
            yaml.safe_dump(self.build_workspace_manifest(), sort_keys=False)
        )
        shutil.copy2(_DEPLOY_CLI, deploy_dir / "deploy.py")
        (deploy_dir / "deploy.py").chmod(0o755)

        # Top-level Makefile + README make the deploy story discoverable.
        service_label = ", ".join(
            sorted({f.deployment for f in self.editable_files})
        ) or "service"
        (host_workspace / "Makefile").write_text(_MAKEFILE_TEMPLATE)
        (host_workspace / "README.md").write_text(
            _README_TEMPLATE.format(service_label=service_label)
        )
        return host_workspace

    def workspace_hint(self) -> str:
        """Short prompt fragment telling the agent the workspace exists.

        Returned string is appended to the mitigation prompt's user-template
        for code-change problems. Config-only problems return ''.
        """
        if not self.has_workspace():
            return ""
        services = sorted({f.deployment for f in self.editable_files})
        services_str = ", ".join(services)
        return (
            "\n\n"
            "The source code for the affected service(s) "
            f"({services_str}) is checked out at /workspace. Edit files there "
            "to fix the bug, then run `make deploy` from /workspace to roll "
            "your changes out. Use `make status` to verify the rollout."
        )

    def requires_khaos(self) -> bool:
        """Override this method to return True if the problem requires Khaos for fault injection."""
        return False

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
