"""Shared fixtures for LLM-as-a-judge tests.

Problem subclasses construct an Application object (e.g. AstronomyShop) that
talks to a real Kubernetes cluster at __init__ time. The tests here only need
the rendered ``root_cause`` string from a Problem instance, so we stub kubectl
out so problems can be instantiated without a live cluster.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_kubectl(monkeypatch):
    monkeypatch.setattr(
        "sregym.service.kubectl.KubeCtl.__init__",
        lambda self: None,
    )
    monkeypatch.setattr(
        "sregym.service.kubectl.KubeCtl.exec_command",
        lambda self, *_args, **_kwargs: "exists",
    )
    monkeypatch.setattr(
        "sregym.service.apps.astronomy_shop.AstronomyShop.create_workload",
        lambda self: None,
    )
