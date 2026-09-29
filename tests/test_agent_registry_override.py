"""Campaign overrides must reach both CLI preparation and agent startup."""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def benchmark_main():
    spec = importlib.util.spec_from_file_location(
        "sregym_main_registry_test", Path(__file__).resolve().parents[1] / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_registry(path, version):
    path.write_text(
        yaml.safe_dump({"agents": [{"name": "codex", "install_script": "install-codex.sh", "agent_version": version}]})
    )


def test_benchmark_uses_override_registry(benchmark_main, tmp_path, monkeypatch):
    write_registry(tmp_path / "agents.yaml", None)
    custom = tmp_path / "pinned-agents.yaml"
    write_registry(custom, "0.157.0")
    monkeypatch.setattr(benchmark_main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setenv("SREGYM_AGENT_REGISTRY", str(custom))

    registration = benchmark_main.get_benchmark_agent("codex")

    assert registration.agent_version == "0.157.0"
    assert registration.install_script == "install-codex.sh"


def test_benchmark_defaults_to_checkout_registry(benchmark_main, tmp_path, monkeypatch):
    write_registry(tmp_path / "agents.yaml", "0.157.1")
    monkeypatch.setattr(benchmark_main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.delenv("SREGYM_AGENT_REGISTRY", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_registry(elsewhere / "agents.yaml", "unrelated")
    monkeypatch.chdir(elsewhere)

    assert benchmark_main.get_benchmark_agent("codex").agent_version == "0.157.1"
