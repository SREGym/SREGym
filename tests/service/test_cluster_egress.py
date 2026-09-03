from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.service.cluster_egress import (
    POLICY_NAME,
    POLICY_PLURAL,
    POLICY_TIER,
    ClusterEgressBoundary,
)


class FakeCustomObjectsAPI:
    def __init__(self):
        self.policy = None
        self.created = []
        self.replaced = []
        self.deleted = []

    def get_cluster_custom_object(self, group, version, plural, name):
        if plural == "tiers" and name == POLICY_TIER:
            return {"metadata": {"name": name}}
        if plural == POLICY_PLURAL and name == POLICY_NAME and self.policy is not None:
            return {"metadata": {"name": name, "resourceVersion": "7"}, **self.policy}
        raise ApiException(status=404)

    def create_cluster_custom_object(self, group, version, plural, body):
        self.created.append((plural, body))
        self.policy = body

    def list_cluster_custom_object(self, group, version, plural):
        assert plural == "ippools"
        return {"items": [{"spec": {"cidr": "10.244.0.0/16"}}]}

    def replace_cluster_custom_object(self, group, version, plural, name, body):
        self.replaced.append((plural, name, body))
        self.policy = body

    def delete_cluster_custom_object(self, group, version, plural, name):
        self.deleted.append((plural, name))
        self.policy = None


class FakeCoreAPI:
    def __init__(self, *, service_subnet="10.96.0.0/12", apiserver_args=None):
        self.service_subnet = service_subnet
        self.apiserver_args = apiserver_args or []

    def list_node(self):
        node = SimpleNamespace(
            spec=SimpleNamespace(pod_cid_rs=["10.244.0.0/16"], pod_cidr="10.244.0.0/16"),
            status=SimpleNamespace(
                addresses=[
                    SimpleNamespace(type="InternalIP", address="192.0.2.10"),
                    SimpleNamespace(type="Hostname", address="worker-0"),
                ]
            ),
        )
        return SimpleNamespace(items=[node])

    def read_namespaced_config_map(self, name, namespace):
        if not self.service_subnet:
            raise ApiException(status=404)
        return SimpleNamespace(
            data={
                "ClusterConfiguration": (
                    f"apiVersion: kubeadm.k8s.io/v1beta4\nnetworking:\n  serviceSubnet: {self.service_subnet}\n"
                )
            }
        )

    def list_namespaced_pod(self, namespace, label_selector):
        container = SimpleNamespace(command=["kube-apiserver"], args=self.apiserver_args)
        return SimpleNamespace(items=[SimpleNamespace(spec=SimpleNamespace(containers=[container]))])


def test_boundary_creates_cluster_wide_deny_then_pass_policy():
    custom_api = FakeCustomObjectsAPI()
    boundary = ClusterEgressBoundary(
        SimpleNamespace(),
        custom_api=custom_api,
        core_api=FakeCoreAPI(),
    )

    boundary.start()

    assert len(custom_api.created) == 1
    body = custom_api.created[0][1]
    assert body["metadata"]["name"] == POLICY_NAME
    assert body["spec"]["tier"] == POLICY_TIER
    assert body["spec"]["selector"] == "all()"
    assert body["spec"]["egress"][-1] == {"action": "Pass"}
    ipv4_rule = body["spec"]["egress"][0]
    assert ipv4_rule["action"] == "Deny"
    assert ipv4_rule["destination"]["nets"] == ["0.0.0.0/0"]
    assert ipv4_rule["destination"]["notNets"] == [
        "10.96.0.0/12",
        "10.244.0.0/16",
        "192.0.2.10/32",
    ]

    boundary.stop()
    assert custom_api.deleted == [(POLICY_PLURAL, POLICY_NAME)]


def test_boundary_replaces_a_stale_policy():
    custom_api = FakeCustomObjectsAPI()
    custom_api.policy = {"spec": {"selector": "old == 'value'"}}
    boundary = ClusterEgressBoundary(
        SimpleNamespace(),
        custom_api=custom_api,
        core_api=FakeCoreAPI(),
    )

    boundary.start()

    assert len(custom_api.replaced) == 1
    body = custom_api.replaced[0][2]
    assert body["metadata"]["resourceVersion"] == "7"
    assert body["spec"]["selector"] == "all()"


def test_boundary_reads_service_subnet_from_apiserver_when_kubeadm_config_is_absent():
    boundary = ClusterEgressBoundary(
        SimpleNamespace(),
        custom_api=FakeCustomObjectsAPI(),
        core_api=FakeCoreAPI(
            service_subnet=None,
            apiserver_args=["--service-cluster-ip-range", "10.97.0.0/16,fd00:10:97::/112"],
        ),
    )

    networks = boundary._internal_networks()

    assert "10.97.0.0/16" in networks
    assert "fd00:10:97::/112" in networks


def test_boundary_fails_closed_when_service_subnet_is_unknown():
    boundary = ClusterEgressBoundary(
        SimpleNamespace(),
        custom_api=FakeCustomObjectsAPI(),
        core_api=FakeCoreAPI(service_subnet=None),
    )

    with pytest.raises(RuntimeError, match="Service subnet"):
        boundary.start()
