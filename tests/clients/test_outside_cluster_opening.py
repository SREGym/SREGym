"""Drivers open a problem outside the test cluster without naming the cluster or its tools.

The fixtures hold each driver's prompt for a cluster problem, which must stay
byte-identical.
"""

import importlib
from pathlib import Path

import pytest

from clients.claudecode_radar import driver as radar
from clients.harness.environment import require_cluster
from clients.jev.config import MODEL_ENV

FIXTURES = Path(__file__).parent / "fixtures" / "cluster_prompts"
DRIVERS = ["claudecode", "codex", "opencode", "geminicli", "cursor", "copilot"]
APP_INFO = {
    "app_name": "test-app",
    "namespace": "test-ns",
    "descriptions": "Test description.",
    "stages": ["diagnosis", "mitigation"],
}


@pytest.fixture(autouse=True)
def fixed_env(monkeypatch):
    monkeypatch.setenv("API_HOSTNAME", "localhost")
    monkeypatch.setenv("API_PORT", "8000")
    monkeypatch.delenv(MODEL_ENV, raising=False)
    monkeypatch.delenv("CHIRON_MCP_URL", raising=False)


def build(driver: str, **extra) -> str:
    # Importing the Radar client replaces Claude Code's build_instruction; test the original.
    if driver == "claudecode":
        return radar._upstream_build_instruction({**APP_INFO, **extra})
    return importlib.import_module(f"clients.{driver}.driver").build_instruction({**APP_INFO, **extra})


@pytest.mark.parametrize("driver", DRIVERS)
@pytest.mark.parametrize("extra", [{}, {"uses_cluster": True}])
def test_cluster_prompt_is_unchanged(driver, extra):
    assert build(driver, **extra) == (FIXTURES / f"{driver}.txt").read_text()


@pytest.mark.parametrize("driver", DRIVERS)
def test_outside_cluster_prompt_names_no_cluster_tools(driver):
    prompt = build(driver, uses_cluster=False)
    assert prompt.startswith(
        "You are an SRE agent tasked with diagnosing and fixing issues in an application.\n\n"
        "Application: test-app\n\nTest description.\n"
    )
    assert "- Use the access and tools described above to inspect and change the application\n" in prompt
    for word in ("Kubernetes", "kubectl", "amespace", "test-ns", "observability"):
        assert word not in prompt
    # The workflow and submission text is the cluster prompt's own.
    assert "TASK 1: DIAGNOSIS" in prompt
    assert "/submit" in prompt


def test_cluster_only_clients_refuse_a_problem_outside_the_cluster():
    require_cluster(APP_INFO, "Client")
    require_cluster({**APP_INFO, "uses_cluster": True}, "Client")
    with pytest.raises(SystemExit, match="Client needs the test cluster"):
        require_cluster({**APP_INFO, "uses_cluster": False}, "Client")

    with pytest.raises(SystemExit, match="Radar needs the test cluster"):
        radar.build_instruction({**APP_INFO, "uses_cluster": False})
