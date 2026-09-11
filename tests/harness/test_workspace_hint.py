from pathlib import Path

from clients.harness.workspace import append_workspace_hint
from sregym.agent_launcher import AgentLauncher


def test_append_workspace_hint_skips_empty():
    assert append_workspace_hint("hello", {}) == "hello"
    assert append_workspace_hint("hello", {"workspace_hint": "  "}) == "hello"


def test_append_workspace_hint_adds_paragraph():
    out = append_workspace_hint("hello\n", {"workspace_hint": "See /workspace"})
    assert out.endswith("See /workspace\n")
    assert out.startswith("hello")


def test_launcher_problem_workspace_defaults_to_none_and_can_be_cleared():
    launcher = AgentLauncher()
    assert launcher._problem_workspace is None
    workspace = Path("/tmp/sregym-workspace")
    launcher.set_problem_workspace(workspace)
    assert launcher._problem_workspace == workspace
    launcher.set_problem_workspace(None)
    assert launcher._problem_workspace is None
