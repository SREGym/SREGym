from __future__ import annotations

from pathlib import Path

import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner


def test_real_kubeconfig_prefers_explicit_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "selected-config"
    selected.write_text("selected", encoding="utf-8")
    fallback_home = tmp_path / "home"
    fallback = fallback_home / ".kube" / "config"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("fallback", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fallback_home))
    monkeypatch.setenv("KUBECONFIG", str(selected))

    args = ContainerRunner(ContainerConfig())._build_base_docker_args()

    assert f"{selected.resolve()}:/root/.kube/real-config:ro" in args
    assert f"{fallback.resolve()}:/root/.kube/real-config:ro" not in args


def test_real_kubeconfig_rejects_ambiguous_path_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBECONFIG", f"{tmp_path / 'one'}:{tmp_path / 'two'}")

    with pytest.raises(ValueError, match="one explicit KUBECONFIG"):
        ContainerRunner(ContainerConfig())._build_base_docker_args()