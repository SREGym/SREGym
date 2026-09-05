"""Cluster-wide outbound network boundary for evaluated workloads."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.infra.cluster_egress")

CALICO_API_GROUP = "crd.projectcalico.org"
CALICO_API_VERSION = "v1"
POLICY_PLURAL = "globalnetworkpolicies"
TIER_PLURAL = "tiers"
IP_POOL_PLURAL = "ippools"
POLICY_TIER = "adminnetworkpolicy"
POLICY_NAME = f"{POLICY_TIER}.external-egress-boundary"
POLICY_LABEL_KEY = "network-access"
POLICY_LABEL_VALUE = "restricted"


class ClusterEgressBoundary:
    """Deny pod traffic to addresses outside the active Kubernetes cluster.

    The policy ends with Calico's ``Pass`` action. Internal traffic therefore
    continues through the normal policy tiers, where fault-specific Kubernetes
    NetworkPolicies can still allow or deny it.
    """

    def __init__(
        self,
        kubectl: KubeCtl,
        *,
        custom_api: client.CustomObjectsApi | None = None,
        core_api: client.CoreV1Api | None = None,
    ) -> None:
        self.kubectl = kubectl
        self.custom_api = custom_api or client.CustomObjectsApi()
        self.core_api = core_api or kubectl.core_v1_api
        self._active = False

    def start(self) -> None:
        """Create or replace the outbound boundary and fail closed on errors."""
        self._require_calico_tier()
        internal_networks = self._internal_networks()
        body = self._policy_body(internal_networks)

        try:
            existing = self.custom_api.get_cluster_custom_object(
                CALICO_API_GROUP,
                CALICO_API_VERSION,
                POLICY_PLURAL,
                POLICY_NAME,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise RuntimeError("Could not read the cluster outbound policy") from exc
            self.custom_api.create_cluster_custom_object(
                CALICO_API_GROUP,
                CALICO_API_VERSION,
                POLICY_PLURAL,
                body,
            )
            logger.info("Created cluster outbound policy %s", POLICY_NAME)
        else:
            body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            self.custom_api.replace_cluster_custom_object(
                CALICO_API_GROUP,
                CALICO_API_VERSION,
                POLICY_PLURAL,
                POLICY_NAME,
                body,
            )
            logger.info("Replaced cluster outbound policy %s", POLICY_NAME)

        self._active = True
        logger.info("Cluster outbound policy permits only: %s", ", ".join(internal_networks))

    def stop(self) -> None:
        """Remove the outbound boundary, including one left by an interrupted run."""
        try:
            self.custom_api.delete_cluster_custom_object(
                CALICO_API_GROUP,
                CALICO_API_VERSION,
                POLICY_PLURAL,
                POLICY_NAME,
            )
            logger.info("Removed cluster outbound policy %s", POLICY_NAME)
        except ApiException as exc:
            if exc.status != 404:
                raise RuntimeError("Could not remove the cluster outbound policy") from exc
        finally:
            self._active = False

    def _require_calico_tier(self) -> None:
        try:
            self.custom_api.get_cluster_custom_object(
                CALICO_API_GROUP,
                CALICO_API_VERSION,
                TIER_PLURAL,
                POLICY_TIER,
            )
        except ApiException as exc:
            raise RuntimeError(
                "Filtered internet access requires Calico 3.29 or later with the adminnetworkpolicy tier. "
                "Upgrade older kind clusters using the Calico version in kind/setup_kind_cluster.sh."
            ) from exc

    def _internal_networks(self) -> list[str]:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

        for node in self.core_api.list_node().items:
            # The generated Kubernetes client spells the ``podCIDRs`` field
            # ``pod_cid_rs``. Fall back to the singular field for older APIs.
            pod_cidrs = node.spec.pod_cid_rs or ([node.spec.pod_cidr] if node.spec.pod_cidr else [])
            networks.extend(_parse_networks(pod_cidrs))
            for address in node.status.addresses or []:
                if address.type in {"InternalIP", "ExternalIP"}:
                    networks.append(ipaddress.ip_network(address.address, strict=False))

        networks.extend(_parse_networks(self._calico_pod_networks()))
        networks.extend(_parse_networks(self._service_subnets()))
        if not networks:
            raise RuntimeError("Could not discover any internal Kubernetes networks")

        collapsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for version in (4, 6):
            same_version = [network for network in networks if network.version == version]
            collapsed.extend(ipaddress.collapse_addresses(same_version))
        return [str(network) for network in collapsed]

    def _calico_pod_networks(self) -> list[str]:
        try:
            response = self.custom_api.list_cluster_custom_object(
                CALICO_API_GROUP,
                CALICO_API_VERSION,
                IP_POOL_PLURAL,
            )
        except ApiException as exc:
            raise RuntimeError("Could not discover the Calico pod networks") from exc
        return [
            cidr for item in response.get("items", []) if isinstance(cidr := (item.get("spec") or {}).get("cidr"), str)
        ]

    def _service_subnets(self) -> list[str]:
        subnets = self._service_subnets_from_kubeadm()
        if subnets:
            return subnets
        subnets = self._service_subnets_from_apiserver()
        if subnets:
            return subnets
        raise RuntimeError(
            "Could not discover the Kubernetes Service subnet. "
            "Filtered internet access cannot safely distinguish cluster traffic from internet traffic."
        )

    def _service_subnets_from_kubeadm(self) -> list[str]:
        try:
            config_map = self.core_api.read_namespaced_config_map("kubeadm-config", "kube-system")
        except ApiException as exc:
            if exc.status == 404:
                return []
            raise

        raw = (config_map.data or {}).get("ClusterConfiguration")
        if not raw:
            return []
        data = yaml.safe_load(raw) or {}
        service_subnet = (data.get("networking") or {}).get("serviceSubnet")
        return _split_cidrs(service_subnet)

    def _service_subnets_from_apiserver(self) -> list[str]:
        try:
            pods = self.core_api.list_namespaced_pod(
                "kube-system",
                label_selector="component=kube-apiserver",
            ).items
        except ApiException:
            return []

        for pod in pods:
            for container in pod.spec.containers or []:
                arguments = [*(container.command or []), *(container.args or [])]
                for index, argument in enumerate(arguments):
                    prefix = "--service-cluster-ip-range="
                    if argument.startswith(prefix):
                        return _split_cidrs(argument.removeprefix(prefix))
                    if argument == "--service-cluster-ip-range" and index + 1 < len(arguments):
                        return _split_cidrs(arguments[index + 1])
        return []

    def _policy_body(self, internal_networks: list[str]) -> dict:
        ipv4 = [network for network in internal_networks if ipaddress.ip_network(network).version == 4]
        ipv6 = [network for network in internal_networks if ipaddress.ip_network(network).version == 6]
        rules: list[dict] = []
        if ipv4:
            rules.append(
                {
                    "action": "Deny",
                    "ipVersion": 4,
                    "destination": {"nets": ["0.0.0.0/0"], "notNets": ipv4},
                }
            )
        if ipv6:
            rules.append(
                {
                    "action": "Deny",
                    "ipVersion": 6,
                    "destination": {"nets": ["::/0"], "notNets": ipv6},
                }
            )
        rules.append({"action": "Pass"})

        return {
            "apiVersion": f"{CALICO_API_GROUP}/{CALICO_API_VERSION}",
            "kind": "GlobalNetworkPolicy",
            "metadata": {
                "name": POLICY_NAME,
                "labels": {POLICY_LABEL_KEY: POLICY_LABEL_VALUE},
            },
            "spec": {
                "tier": POLICY_TIER,
                "order": 900,
                "selector": "all()",
                "types": ["Egress"],
                "egress": rules,
            },
        }


def _split_cidrs(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_networks(values: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(value, strict=False) for value in values]
