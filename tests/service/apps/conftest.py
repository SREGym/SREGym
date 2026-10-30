"""Explicit offline dependencies for application unit tests."""

from unittest.mock import Mock

import pytest


@pytest.fixture
def offline_kubectl(monkeypatch):
    from sregym.service.kubectl import KubeCtl

    def initialize(kubectl):
        kubectl.core_v1_api = Mock()
        kubectl.apps_v1_api = Mock()

    # Retain the wrapper's methods; accidental CLI execution still hits the guard.
    monkeypatch.setattr(KubeCtl, "__init__", initialize)
