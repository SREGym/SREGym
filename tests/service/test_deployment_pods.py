from types import SimpleNamespace as Obj
from unittest.mock import Mock

from sregym.service.kubectl import KubeCtl


def owner(kind, uid, controller=True):
    return Obj(kind=kind, uid=uid, controller=controller)


def resource(uid, owners):
    return Obj(metadata=Obj(uid=uid, owner_references=owners))


def test_deployment_pods_follow_controller_uids_not_names_or_labels():
    kubectl = object.__new__(KubeCtl)
    deployment = Obj(metadata=Obj(name="kafka", uid="current-deployment"))
    sets = [
        resource("old-rs", [owner("Deployment", "current-deployment")]),
        resource("new-rs", [owner("Deployment", "current-deployment")]),
        resource("stale-rs", [owner("Deployment", "deleted-deployment")]),
        resource("uncontrolled-rs", [owner("Deployment", "current-deployment", False)]),
    ]
    kubectl.get_matching_replicasets = Mock(return_value=sets)
    pods = [
        resource("old-pod", [owner("ReplicaSet", "old-rs")]),
        resource("new-pod", [owner("ReplicaSet", "new-rs")]),
        resource("stale-pod", [owner("ReplicaSet", "stale-rs")]),
        resource("uncontrolled-pod", [owner("ReplicaSet", "uncontrolled-rs")]),
        resource("fake-pod", [owner("ReplicaSet", "new-rs", False)]),
        resource("bare-pod", []),
    ]
    kubectl.list_pods = Mock(return_value=Obj(items=pods))
    assert kubectl.get_deployment_pods(deployment, "app") == pods[:2]
    kubectl.get_matching_replicasets.assert_called_once_with("app", "kafka")
