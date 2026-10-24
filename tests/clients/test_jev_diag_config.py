"""Jev diagnosis settings survive the normal agent-container launch."""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from clients.jev_diag import config, driver
from sregym.service.container_runner import ContainerConfig, ContainerRunner


def test_every_diagnosis_setting_is_declared_for_container_forwarding():
    root = Path(config.__file__).parent
    names = {name for path in root.rglob("*.py") for name in re.findall(r"\bJEV_DIAG_[A-Z0-9_]+\b", path.read_text())}
    assert names <= set(config.ENV_VARS)


@pytest.mark.parametrize("mode", ["tree", "oneshot", "v2"])
def test_container_launch_preserves_mode_and_v2_disable_switches(monkeypatch, mode):
    monkeypatch.setenv("AGENT_JEV_MODEL", "jev-test")
    settings = {
        "JEV_DIAG_MODE": mode,
        "JEV_DIAG_V2_PROBES": "0",
        "JEV_DIAG_V2_TELEMETRY": "0",
        "JEV_DIAG_IGNORE_NAMESPACES": "unrelated,monitoring",
        "JEV_DIAG_START_DELAY": "0",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    env = ContainerRunner(ContainerConfig())._build_env_vars()
    assert {name: env[name] for name in settings} == settings
    with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["jev_diag"]):
        args = driver.parse_args()
    assert args.mode == mode and args.start_delay == 0


def test_forwarded_settings_keep_explicit_container_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_JEV_MODEL", "jev-test")
    for name in config.ENV_VARS:
        monkeypatch.setenv(name, f"review-{name}")
    runner = ContainerRunner(ContainerConfig(env_vars={"JEV_DIAG_MODE": "oneshot"}))
    env = runner._build_env_vars()
    assert env["JEV_DIAG_MODE"] == "oneshot"
    assert all(env[name] == f"review-{name}" for name in config.ENV_VARS if name != "JEV_DIAG_MODE")


def test_diagnosis_settings_are_not_added_to_non_jev_runs(monkeypatch):
    monkeypatch.delenv("AGENT_JEV_MODEL", raising=False)
    monkeypatch.setenv("JEV_DIAG_MODE", "v2")
    monkeypatch.setenv("JEV_DIAG_V2_PROBES", "0")
    env = ContainerRunner(ContainerConfig())._build_env_vars()
    assert "JEV_DIAG_MODE" not in env and "JEV_DIAG_V2_PROBES" not in env
