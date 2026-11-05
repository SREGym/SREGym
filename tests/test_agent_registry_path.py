from pathlib import Path

import pytest

from sregym.agent_registry import REGISTRY_ENV, list_agents, registry_path


def test_defaults_to_agents_yaml_under_repo_root(tmp_path, monkeypatch):
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    assert registry_path(tmp_path) == tmp_path / "agents.yaml"


def test_blank_override_falls_back_to_repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv(REGISTRY_ENV, "  ")
    assert registry_path(tmp_path) == tmp_path / "agents.yaml"


def test_override_is_read_at_call_time(tmp_path, monkeypatch):
    # Set after sregym.agent_registry was imported, the way a wrapper would.
    override = tmp_path / "elsewhere" / "registry.yaml"
    override.parent.mkdir()
    override.write_text("agents: []\n")
    monkeypatch.setenv(REGISTRY_ENV, str(override))
    assert registry_path(tmp_path) == override


def test_override_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "registry.yaml").write_text("agents: []\n")
    monkeypatch.setenv(REGISTRY_ENV, "~/registry.yaml")
    assert registry_path(Path("/unused")) == tmp_path / "registry.yaml"


def test_missing_override_fails_without_creating_a_file(tmp_path, monkeypatch):
    missing = tmp_path / "typo.yaml"
    monkeypatch.setenv(REGISTRY_ENV, str(missing))
    with pytest.raises(FileNotFoundError, match=REGISTRY_ENV):
        registry_path(tmp_path)
    assert not missing.exists()


def test_override_replaces_the_repo_registry(tmp_path, monkeypatch):
    (tmp_path / "agents.yaml").write_text("agents:\n  - name: in_repo\n    kickoff_command: x\n")
    override = tmp_path / "external.yaml"
    override.write_text("agents:\n  - name: external\n    kickoff_command: y\n    container_isolation: false\n")
    monkeypatch.setenv(REGISTRY_ENV, str(override))

    agents = list_agents(registry_path(tmp_path))

    assert set(agents) == {"external"}
    assert agents["external"].container_isolation is False
