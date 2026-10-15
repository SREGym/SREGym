import json
from unittest.mock import Mock

import pytest

from sregym.observer.ingress_nginx import IngressNginx


def resource(name, namespace, release="ingress-nginx"):
    return {
        "kind": "ClusterRole",
        "metadata": {
            "name": name,
            "annotations": {"meta.helm.sh/release-name": release, "meta.helm.sh/release-namespace": namespace},
        },
    }


def test_cleanup_checks_both_helm_ownership_annotations():
    ingress = IngressNginx()
    objects = [
        resource("owned", ingress.namespace),
        resource("other-namespace", "other"),
        resource("other-release", ingress.namespace, "other"),
        {"kind": "ClusterRole", "metadata": {"name": "unowned"}},
    ]
    ingress.run_cmd = Mock(side_effect=["[]", json.dumps({"items": objects}), ""])
    ingress._remove_orphaned_cluster_objects()
    commands = [call.args[0] for call in ingress.run_cmd.call_args_list]
    assert commands[-1] == "kubectl delete clusterrole/owned --ignore-not-found"
    assert len(commands) == 3


def test_existing_release_keeps_its_cluster_objects():
    ingress = IngressNginx()
    ingress.run_cmd = Mock(return_value='[{"name":"ingress-nginx"}]')
    ingress._remove_orphaned_cluster_objects()
    ingress.run_cmd.assert_called_once()


@pytest.mark.parametrize("responses", [[RuntimeError("Helm unavailable")], ["[]", RuntimeError("API unavailable")]])
def test_transport_errors_never_trigger_deletion(responses):
    ingress = IngressNginx()
    ingress.run_cmd = Mock(side_effect=responses)
    with pytest.raises(RuntimeError):
        ingress._remove_orphaned_cluster_objects()
    assert not any(call.args[0].startswith("kubectl delete") for call in ingress.run_cmd.call_args_list)
