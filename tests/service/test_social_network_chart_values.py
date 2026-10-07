"""The Social Network chart's fixed MAC address: only set when requested."""

from sregym.service.apps.social_network import chart_set_values


def test_no_values_by_default(monkeypatch):
    monkeypatch.delenv("SREGYM_FIXED_MAC_ADDRESS", raising=False)
    assert chart_set_values() == {}


def test_fixed_mac_address_from_the_environment(monkeypatch):
    monkeypatch.setenv("SREGYM_FIXED_MAC_ADDRESS", " 02:42:ac:11:00:02 ")
    assert chart_set_values() == {"global.fixedMacAddress": "02:42:ac:11:00:02"}
