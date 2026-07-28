from __future__ import annotations

from pathlib import Path

import yaml

from sregym.generators.fault.inject_remote_os import RemoteOSFaultInjector


def _inventory(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "all": {
                    "vars": {"k8s_user2": "fdaiops"},
                    "children": {
                        "worker_nodes": {
                            "hosts": {
                                "fdai-bench-wk-01": {
                                    "ansible_host": "10.72.0.11",
                                    "ansible_user": "{{ k8s_user2 }}",
                                },
                                "fdai-bench-wk-02": {
                                    "ansible_host": "10.72.0.12",
                                    "ansible_user": "{{ k8s_user2 }}",
                                },
                            }
                        }
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_node_exec_uses_exact_inventory_alias(tmp_path: Path) -> None:
    injector = RemoteOSFaultInjector(inventory_path=_inventory(tmp_path / "inventory.yml"))
    calls: list[tuple[str, str, str]] = []
    injector._ssh_exec = lambda host, user, command: calls.append((host, user, command)) or "ok"  # type: ignore[method-assign]

    assert injector._node_exec("fdai-bench-wk-02", "systemctl stop kubelet") == "ok"
    assert calls == [("10.72.0.12", "fdaiops", "sudo sh -c 'systemctl stop kubelet'")]


def test_node_exec_rejects_unknown_node_without_fallback(tmp_path: Path) -> None:
    injector = RemoteOSFaultInjector(inventory_path=_inventory(tmp_path / "inventory.yml"))
    calls: list[tuple[str, str, str]] = []
    injector._ssh_exec = lambda host, user, command: calls.append((host, user, command)) or "ok"  # type: ignore[method-assign]

    assert injector._node_exec("unknown-worker", "true") == ""
    assert calls == []