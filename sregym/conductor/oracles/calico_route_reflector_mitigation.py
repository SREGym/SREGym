"""Mitigation oracle for Calico route-reflector label drift failures."""

import json
import re
import shlex
import subprocess
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.base import Oracle

_ROLLOUT_SETTLE_SECONDS = 180
_ROLLOUT_POLL_INTERVAL = 5


class CalicoRouteReflectorMitigationOracle(Oracle):
    """Verify recovery came from fixing Calico route-reflector selection."""

    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()

    @staticmethod
    def _q(value):
        return shlex.quote(str(value))

    def _run(self, command, timeout=20):
        try:
            return subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(
                command,
                124,
                stdout=e.stdout or "",
                stderr=f"Timed out after {timeout}s",
            )

    def _wait_for_rollouts(self, namespace):
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = self.apps_v1.list_namespaced_deployment(namespace)
            all_settled = True
            for dep in deployments.items:
                desired = dep.spec.replicas or 0
                status = dep.status
                if (
                    (status.updated_replicas or 0) < desired
                    or (status.ready_replicas or 0) < desired
                    or (status.unavailable_replicas or 0) > 0
                ):
                    all_settled = False
                    break
            if all_settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print(f"WARNING: Timed out waiting for deployments in namespace '{namespace}' to settle")

    def _deployments_ready(self, namespace):
        try:
            deployments = self.apps_v1.list_namespaced_deployment(namespace).items
        except ApiException as e:
            if e.status == 404:
                print(f"FAIL: Namespace '{namespace}' not found")
                return False
            raise

        if not deployments:
            print(f"FAIL: No deployments found in namespace '{namespace}'")
            return False

        for deployment in deployments:
            desired = deployment.spec.replicas or 0
            ready = deployment.status.ready_replicas or 0
            if desired < 1 or ready != desired:
                print(f"FAIL: Deployment '{namespace}/{deployment.metadata.name}' has {ready}/{desired} replicas ready")
                return False
        return True

    def _app_replicas_not_reduced(self, namespace):
        expected_replicas = getattr(self.problem, "_app_deployment_replicas", {}) or {}
        if not expected_replicas:
            return True

        deployments = {
            deployment.metadata.name: deployment
            for deployment in self.apps_v1.list_namespaced_deployment(namespace).items
        }
        for name, expected in expected_replicas.items():
            deployment = deployments.get(name)
            if deployment is None:
                print(f"FAIL: Deployment '{namespace}/{name}' is missing")
                return False
            desired = deployment.spec.replicas or 0
            if desired < expected:
                print(
                    f"FAIL: Deployment '{namespace}/{name}' was scaled down " f"from {expected} to {desired} replicas"
                )
                return False
        return True

    def _application_spans_multiple_nodes(self, namespace):
        try:
            pods = self.core_v1.list_namespaced_pod(namespace).items
        except ApiException as e:
            if e.status == 404:
                print(f"FAIL: Namespace '{namespace}' not found")
                return False
            raise

        running_nodes = {
            pod.spec.node_name
            for pod in pods
            if getattr(pod.status, "phase", None) == "Running" and getattr(pod.spec, "node_name", None)
        }
        if len(running_nodes) < 2:
            print("FAIL: Hotel Reservation pods do not span multiple nodes")
            return False
        return True

    def _calico_ready(self):
        result = self._run("kubectl -n kube-system rollout status ds/calico-node --timeout=30s", timeout=40)
        if result.returncode != 0:
            print(f"FAIL: Calico node DaemonSet is not ready: {result.stderr.strip()}")
            return False
        return True

    def _probe_pod(self, app):
        namespace = self.problem.PROBE_NAMESPACE
        result = self._run(
            f"kubectl -n {self._q(namespace)} get pod -l app={self._q(app)} -o json",
            timeout=20,
        )
        if result.returncode != 0:
            print(f"FAIL: Probe pod for app={app} is missing: {result.stderr.strip()}")
            return None
        pods = json.loads(result.stdout).get("items", [])
        if len(pods) != 1:
            print(f"FAIL: Expected exactly one probe pod for app={app}; found {len(pods)}")
            return None
        pod = pods[0]
        return {
            "name": pod.get("metadata", {}).get("name"),
            "node": pod.get("spec", {}).get("nodeName"),
            "ip": pod.get("status", {}).get("podIP"),
        }

    def _probe_topology(self):
        client_pod = self._probe_pod(self.problem.PROBE_CLIENT)
        same_node_server = self._probe_pod(self.problem.PROBE_LOCAL_SERVER)
        cross_node_server = self._probe_pod(self.problem.PROBE_SERVER)
        if not client_pod or not same_node_server or not cross_node_server:
            return None
        if not client_pod["node"] or not same_node_server["node"] or not cross_node_server["node"]:
            print("FAIL: Probe pod node placement is not available")
            return None
        if not same_node_server["ip"] or not cross_node_server["ip"]:
            print("FAIL: Probe server pod IP is not available")
            return None
        if client_pod["node"] != same_node_server["node"]:
            print("FAIL: Same-node probe server is not colocated with the probe client")
            return None
        if client_pod["node"] == cross_node_server["node"]:
            print("FAIL: Cross-node probe server is colocated with the probe client")
            return None
        return {
            "client": client_pod,
            "same_node_server": same_node_server,
            "cross_node_server": cross_node_server,
        }

    def _probe_url_from_client_ok(self, client_pod, url, description, expect_body=None):
        probe = self._run(
            f"kubectl -n {self._q(self.problem.PROBE_NAMESPACE)} exec {self._q(client_pod)} -- "
            f"wget -T 3 -qO- {self._q(url)}",
            timeout=10,
        )
        if probe.returncode != 0:
            print(f"FAIL: {description} failed: {probe.stderr.strip()}")
            return False
        if expect_body is not None and probe.stdout.strip() != expect_body:
            print(f"FAIL: {description} returned unexpected body: {probe.stdout.strip()!r}")
            return False
        return True

    def _cross_node_probe_ok(self):
        topology = self._probe_topology()
        if topology is None:
            return False

        client_pod = topology["client"]["name"]
        same_node_server = topology["same_node_server"]
        cross_node_server = topology["cross_node_server"]
        checks = [
            (
                f"http://{same_node_server['ip']}:8080",
                "same-node Pod IP probe",
                "ok",
            ),
            (
                f"http://{cross_node_server['ip']}:8080",
                "cross-node Pod IP probe",
                "ok",
            ),
            (
                f"http://{self.problem.PROBE_SERVER}:8080",
                "cross-node Service/DNS probe",
                "ok",
            ),
        ]
        for url, description, expected_body in checks:
            if not self._probe_url_from_client_ok(client_pod, url, description, expect_body=expected_body):
                return False
        return True

    def _hotel_reservation_request_ok(self):
        topology = self._probe_topology()
        if topology is None:
            return False

        frontend_service = getattr(self.problem.app, "frontend_service", "frontend")
        frontend_port = getattr(self.problem.app, "frontend_port", 5000)
        url = f"http://{frontend_service}.{self.problem.namespace}.svc.cluster.local:{frontend_port}/"
        return self._probe_url_from_client_ok(
            topology["client"]["name"],
            url,
            "Hotel Reservation frontend request probe",
        )

    def _nodes_with_label(self, label, value=None):
        nodes = []
        for node in self.core_v1.list_node().items:
            labels = node.metadata.labels or {}
            if label not in labels:
                continue
            if value is not None and labels.get(label) != value:
                continue
            nodes.append(node.metadata.name)
        return nodes

    def _positive_has_labels(self, selector):
        labels = []
        pattern = re.compile(r"has\(\s*([A-Za-z0-9_.\-/]+)\s*\)")
        for match in pattern.finditer(selector or ""):
            prefix = (selector or "")[: match.start()].rstrip()
            if not prefix.endswith("!"):
                labels.append(match.group(1))
        return labels

    def _positive_equality_labels(self, selector):
        matches = []
        pattern = re.compile(r"([A-Za-z0-9_.\-/]+)\s*(==|=)\s*" r"(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9_.\-/:]+))")
        for match in pattern.finditer(selector or ""):
            prefix = (selector or "")[: match.start()].rstrip()
            if prefix.endswith("!"):
                continue
            value = next(group for group in match.groups()[2:] if group is not None)
            matches.append((match.group(1), value))
        return matches

    def _selector_has_label(self, selector, label):
        return label in self._positive_has_labels(selector)

    def _nodes_selected_by_peer_selector(self, selector):
        selected = set()
        node_items = self.core_v1.list_node().items
        for clause in re.split(r"\s*\|\|\s*", selector or ""):
            required_labels = self._positive_has_labels(clause)
            required_equalities = self._positive_equality_labels(clause)
            if not required_labels and not required_equalities:
                continue

            for node in node_items:
                labels = node.metadata.labels or {}
                if any(label not in labels for label in required_labels):
                    continue
                if any(labels.get(label) != value for label, value in required_equalities):
                    continue
                selected.add(node.metadata.name)
        return sorted(selected)

    def _selected_route_reflector_has_cluster_id(self, selected_nodes):
        selected = set(selected_nodes)
        if not selected:
            return False

        annotation = self.problem.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION
        missing_cluster_id = []
        for node in self.core_v1.list_node().items:
            if node.metadata.name not in selected:
                continue
            if not (node.metadata.annotations or {}).get(annotation):
                missing_cluster_id.append(node.metadata.name)

        if missing_cluster_id:
            print(f"FAIL: Selected route-reflector nodes are missing RouteReflectorClusterID: {missing_cluster_id}")
            return False
        return True

    def _bgp_configuration_is_route_reflector_mode(self):
        result = self._run("kubectl get bgpconfiguration default -o json")
        if result.returncode != 0:
            print("FAIL: Calico BGPConfiguration/default is missing")
            return False
        config = json.loads(result.stdout)
        if config.get("spec", {}).get("nodeToNodeMeshEnabled") is not False:
            print("FAIL: Calico node-to-node mesh is enabled; route-reflector topology was bypassed")
            return False
        return True

    def _route_reflector_peer_selects_nodes(self):
        result = self._run("kubectl get bgppeers -o json")
        if result.returncode != 0:
            print("FAIL: Could not read Calico BGPPeer resources")
            return False

        peers = json.loads(result.stdout).get("items", [])
        if not peers:
            print("FAIL: No Calico BGPPeer resources are configured")
            return False

        legacy_label = self.problem.LEGACY_MASTER_LABEL
        legacy_nodes = self._nodes_with_label(legacy_label)

        stale_unmatched = []
        selected_nodes = set()
        for peer in peers:
            spec = peer.get("spec", {})
            peer_selector = spec.get("peerSelector", "")
            has_legacy_label = self._selector_has_label(peer_selector, legacy_label)

            if has_legacy_label and not legacy_nodes:
                stale_unmatched.append(peer["metadata"]["name"])
            selected_nodes.update(self._nodes_selected_by_peer_selector(peer_selector))

        if stale_unmatched:
            print(f"FAIL: BGPPeer selector still references unmatched legacy master label: {stale_unmatched}")
            return False
        if not selected_nodes:
            print("FAIL: No BGPPeer selects an existing route-reflector node")
            return False
        return self._selected_route_reflector_has_cluster_id(selected_nodes)

    def evaluate(self) -> dict:
        print("== Calico Route Reflector Mitigation Evaluation ==")

        self._wait_for_rollouts(self.problem.namespace)
        self._wait_for_rollouts(self.problem.PROBE_NAMESPACE)

        if not self._app_replicas_not_reduced(self.problem.namespace):
            return {"success": False}
        if not self._deployments_ready(self.problem.namespace):
            return {"success": False}
        if not self._deployments_ready(self.problem.PROBE_NAMESPACE):
            return {"success": False}
        if not self._application_spans_multiple_nodes(self.problem.namespace):
            return {"success": False}
        if not self._calico_ready():
            return {"success": False}
        if not self._bgp_configuration_is_route_reflector_mode():
            return {"success": False}
        if not self._route_reflector_peer_selects_nodes():
            return {"success": False}
        if not self._cross_node_probe_ok():
            return {"success": False}
        if not self._hotel_reservation_request_ok():
            return {"success": False}

        print("PASS: Calico route-reflector selection and cross-node connectivity are healthy")
        return {"success": True}
