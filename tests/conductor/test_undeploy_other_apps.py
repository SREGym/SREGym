"""Before deploying, the conductor removes other registered apps left by an earlier problem."""

import logging
import types

from sregym.conductor.conductor import Conductor


class _Kubectl:
    def __init__(self, present: set[str]):
        self.present = present
        self.deleted: list[str] = []

    def exec_command(self, cmd: str) -> str:
        ns = cmd.split()[3]
        if cmd.startswith("kubectl get namespace"):
            return f"namespace/{ns}" if ns in self.present else ""
        self.deleted.append(ns)
        return ""


def _conductor(app, present: set[str]) -> Conductor:
    c = Conductor.__new__(Conductor)
    c.logger = logging.getLogger("test.undeploy_other_apps")
    c.kubectl = _Kubectl(present)
    c.problem = types.SimpleNamespace(app=app)
    return c


def test_other_apps_namespaces_are_deleted_and_this_one_is_kept():
    c = _conductor(types.SimpleNamespace(namespace="astronomy-shop"), {"astronomy-shop", "train-ticket", "observe"})

    c.undeploy_other_apps()

    assert c.kubectl.deleted == ["train-ticket"]


def test_every_namespace_of_a_composite_app_is_kept():
    app = types.SimpleNamespace(namespace="hotel-reservation", namespaces=["hotel-reservation", "social-network"])
    c = _conductor(app, {"hotel-reservation", "social-network"})

    c.undeploy_other_apps()

    assert c.kubectl.deleted == []
