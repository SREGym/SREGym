import json
from types import SimpleNamespace

from sregym.conductor.problems.base import EditableFile, Problem
from sregym.utils.decorators import mark_fault_injected


class WorkspaceProblem(Problem):
    def __init__(self, app, source_file):
        super().__init__(app=app)
        self.editable_files = [
            EditableFile(
                workspace_path="recommendation_server.py",
                pod_path="/app/recommendation_server.py",
                deployment="recommendation",
                configmap_name="recommendation-src-override",
                host_source=str(source_file),
            )
        ]

    @mark_fault_injected
    def inject_fault(self):
        pass

    @mark_fault_injected
    def recover_fault(self):
        pass


def test_provision_workspace_copies_host_source_and_deploy_helper(tmp_path):
    source = tmp_path / "asset.py"
    source.write_text("print('buggy')\n", encoding="utf-8")
    problem = WorkspaceProblem(SimpleNamespace(namespace="astronomy-shop"), source)

    assert problem.has_workspace() is True
    hint = problem.workspace_hint()
    assert "/workspace" in hint
    assert "make deploy" in hint
    assert "recommendation" in hint

    workspace = problem.provision_workspace(tmp_path / "workspace")
    assert workspace is not None
    assert (workspace / "recommendation_server.py").read_text(encoding="utf-8") == "print('buggy')\n"
    assert (workspace / "Makefile").exists()
    assert (workspace / "README.md").exists()
    assert (workspace / ".deploy" / "deploy.py").exists()

    manifest = json.loads((workspace / ".deploy" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["namespace"] == "astronomy-shop"
    assert manifest["files"] == [
        {
            "workspace_path": "recommendation_server.py",
            "pod_path": "/app/recommendation_server.py",
            "deployment": "recommendation",
            "configmap_name": "recommendation-src-override",
        }
    ]
    assert "host_source" not in manifest["files"][0]


def test_provision_workspace_is_idempotent(tmp_path):
    source = tmp_path / "asset.py"
    source.write_text("first\n", encoding="utf-8")
    problem = WorkspaceProblem(SimpleNamespace(namespace="astronomy-shop"), source)
    workspace = tmp_path / "workspace"
    problem.provision_workspace(workspace)
    (workspace / "stale.txt").write_text("leftover", encoding="utf-8")

    source.write_text("second\n", encoding="utf-8")
    problem.provision_workspace(workspace)

    assert (workspace / "recommendation_server.py").read_text(encoding="utf-8") == "second\n"
    assert not (workspace / "stale.txt").exists()
