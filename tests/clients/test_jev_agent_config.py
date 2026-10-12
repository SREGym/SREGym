"""jev_agent settings: every variable the agent reads reaches its container, and the stop rules follow the config."""

import re
from pathlib import Path

from clients.jev_agent import config

ROOT = Path(__file__).resolve().parents[2]


def _names_read_by_the_agent() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "clients" / "jev_agent").glob("*.py"):
        names |= set(re.findall(r"\bJEV_AGENT_[A-Z_]+\b", path.read_text(encoding="utf-8")))
    return names


def test_every_setting_the_agent_reads_is_forwarded_or_mounted():
    missing = _names_read_by_the_agent() - set(config.ENV_VARS) - set(config.NOT_FORWARDED)
    assert not missing


def test_container_runner_forwards_exactly_the_agent_settings():
    source = (ROOT / "sregym" / "service" / "container_runner.py").read_text(encoding="utf-8")
    forwarded = set(re.findall(r'"(JEV_AGENT_[A-Z_]+)",', source))
    assert forwarded == set(config.ENV_VARS)


def test_stop_thresholds_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("JEV_AGENT_ENOUGH_P", "0.9")
    monkeypatch.setenv("JEV_AGENT_ORIGIN_P_ONCE", "0.95")
    monkeypatch.setenv("JEV_AGENT_STABLE_STEPS", "4")
    cfg = config.load()
    assert (cfg.enough_p, cfg.origin_p_once, cfg.stable_steps) == (0.9, 0.95, 4)
    assert (cfg.origin_p, cfg.submit_p) == (0.6, 0.5)


def test_escalation_is_on_unless_turned_off(monkeypatch):
    monkeypatch.delenv("JEV_AGENT_ESCALATE", raising=False)
    assert config.load().escalate is True
    monkeypatch.setenv("JEV_AGENT_ESCALATE", "0")
    assert config.load().escalate is False


def test_review_is_on_unless_turned_off(monkeypatch):
    monkeypatch.delenv("JEV_AGENT_REVIEW", raising=False)
    assert config.load().review is True
    monkeypatch.setenv("JEV_AGENT_REVIEW", "off")
    assert config.load().review is False
