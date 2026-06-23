"""Problem: Calico route-reflector node-label drift partitions pod networking.

This models a cluster-networking outage where a Calico route-reflector topology
depends on the deprecated Kubernetes `node-role.kubernetes.io/master` label.
After an upgrade-style label migration removes that label, BGPPeer selectors no
longer match the intended route-reflector node while node-to-node mesh remains
disabled. Cross-node pod/service traffic fails even though application pods and
services still look mostly healthy.
"""

import contextlib
import json
import os
import shlex
import subprocess
import tempfile
import time

from kubernetes import client

from sregym.conductor.oracles.calico_route_reflector_mitigation import (
    CalicoRouteReflectorMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class CalicoRouteReflectorLabelDriftHotelReservation(Problem):
    """Inject stale Calico route-reflector selection after node-label migration."""

    LEGACY_MASTER_LABEL = "node-role.kubernetes.io/master"
    CURRENT_CONTROL_PLANE_LABEL = "node-role.kubernetes.io/control-plane"
    ROUTE_REFLECTOR_CLUSTER_ID = "244.0.0.1"
    ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION = "projectcalico.org/RouteReflectorClusterID"
    BGP_PEER_NAME = "cluster-peer-policy"
    PROBLEM_LABEL_KEY = "platform.example.com/managed-by"
    PROBLEM_LABEL_VALUE = "network-case"
    NODE_MARKER_ANNOTATION = "platform.example.com/network-case-node"
    STATE_NAMESPACE = "khaos"
    STATE_CONFIGMAP_NAME = "network-case-state"
    STATE_CONFIG_PREEXISTED_KEY = "config_preexisted"
    STATE_CONFIGURATION_KEY = "original_config.json"
    STATE_BGP_PEERS_KEY = "original_bgp_peers.json"
    STATE_PRIMARY_NODE_KEY = "primary_node"
    STATE_NODE_LABEL_PREEXISTED_KEY = "node_label_preexisted"
    STATE_NODE_ANNOTATION_PREEXISTED_KEY = "node_annotation_preexisted"
    STATE_NODE_ANNOTATION_VALUE_KEY = "node_annotation_value"
    MIN_WORKER_NODES = 2
    CLUSTER_REQUIREMENTS = (
        "Requires a disposable multi-node Calico cluster with BGP CRDs, one control-plane node, "
        "and at least two worker nodes."
    )

    PROBE_NAMESPACE = "platform-checks"
    PROBE_SERVER = "remote-check"
    PROBE_CLIENT = "check-client"
    PROBE_LOCAL_SERVER = "local-check"

    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.route_reflector_node = None
        self.worker_nodes = []
        self.original_bgp_configuration = None
        self._original_bgppeer_names = None
        self._bgp_config_preexisted = None
        self._legacy_label_preexisted = None
        self._route_reflector_annotation_preexisted = None
        self._route_reflector_annotation_value = None
        self._app_deployment_replicas = {}
        self._app_cleanup = self.app.cleanup
        self.app.cleanup = self._cleanup

        self.root_cause = self.build_structured_root_cause(
            component=f"BGPPeer/{self.BGP_PEER_NAME}",
            namespace="cluster-scoped",
            description=(
                "Calico is running in route-reflector mode with node-to-node mesh disabled. "
                f"The BGPPeer selector still targets the deprecated `{self.LEGACY_MASTER_LABEL}` label, "
                f"but the intended control-plane route-reflector node only has `{self.CURRENT_CONTROL_PLANE_LABEL}`. "
                "Because no route-reflector node is selected, Calico stops propagating cross-node routes. "
                f"Pods in `{self.namespace}` remain mostly Running, but cross-node service/DNS traffic fails. "
                "Mitigation must repair the Calico route-reflector selector or intentionally restore the expected "
                "route-reflector label while preserving the route-reflector topology."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = CalicoRouteReflectorMitigationOracle(problem=self)

    @staticmethod
    def _q(value):
        return shlex.quote(str(value))

    def _run(self, command, check=True, timeout=120):
        try:
            result = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            if check:
                raise
            return subprocess.CompletedProcess(
                command,
                124,
                stdout=e.stdout or "",
                stderr=f"Timed out after {timeout}s",
            )
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {command}\nstderr:\n{result.stderr.strip()}")
        return result

    def _apply_manifest(self, manifest):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(manifest)
            tmp_path = f.name
        try:
            self._run(f"kubectl apply -f {self._q(tmp_path)}")
        finally:
            os.unlink(tmp_path)

    def _calico_available(self):
        result = self._run(
            "kubectl get crd bgppeers.crd.projectcalico.org bgpconfigurations.crd.projectcalico.org",
            check=False,
        )
        return result.returncode == 0

    def _calico_bgp_dataplane_available(self):
        pod_result = self._run(
            "kubectl -n kube-system get pod -l k8s-app=calico-node -o jsonpath='{.items[0].metadata.name}'",
            check=False,
            timeout=30,
        )
        if pod_result.returncode != 0 or not pod_result.stdout.strip():
            return False

        pod = self._q(pod_result.stdout.strip())
        result = self._run(
            f"kubectl -n kube-system exec {pod} -c calico-node -- birdcl show status",
            check=False,
            timeout=30,
        )
        return result.returncode == 0

    def _select_nodes(self):
        nodes = self.core_v1.list_node().items
        control_planes = [
            node.metadata.name for node in nodes if self.CURRENT_CONTROL_PLANE_LABEL in (node.metadata.labels or {})
        ]
        workers = [
            node.metadata.name
            for node in nodes
            if self.CURRENT_CONTROL_PLANE_LABEL not in (node.metadata.labels or {})
            and self.LEGACY_MASTER_LABEL not in (node.metadata.labels or {})
        ]
        if not control_planes:
            raise RuntimeError("Calico route-reflector label drift requires a control-plane node")
        if len(control_planes) != 1:
            raise RuntimeError(
                "Calico route-reflector label drift requires exactly one control-plane node; "
                f"found {len(control_planes)}. {self.CLUSTER_REQUIREMENTS}"
            )
        if len(workers) < self.MIN_WORKER_NODES:
            raise RuntimeError(
                "Calico route-reflector label drift requires at least two worker nodes; "
                f"found {len(workers)}. {self.CLUSTER_REQUIREMENTS}"
            )
        self.route_reflector_node = control_planes[0]
        self.worker_nodes = workers[: self.MIN_WORKER_NODES]

    def _capture_bgp_configuration(self):
        result = self._run("kubectl get bgpconfiguration default -o json", check=False)
        if result.returncode == 0:
            config = json.loads(result.stdout)
            self._bgp_config_preexisted = True
            self.original_bgp_configuration = {
                "apiVersion": config.get("apiVersion", "crd.projectcalico.org/v1"),
                "kind": "BGPConfiguration",
                "metadata": {
                    "name": "default",
                },
                "spec": config.get("spec", {}),
            }
            return

        stderr = (result.stderr or "").lower()
        if "notfound" in stderr or "not found" in stderr:
            self._bgp_config_preexisted = False
            self.original_bgp_configuration = None
            return

        raise RuntimeError(
            "Could not safely capture existing Calico BGPConfiguration/default; "
            f"refusing to mutate cluster-wide routing state. stderr: {result.stderr.strip()}"
        )

    def _capture_route_reflector_node_state(self):
        node = self.core_v1.read_node(name=self.route_reflector_node)
        labels = node.metadata.labels or {}
        annotations = node.metadata.annotations or {}

        self._legacy_label_preexisted = self.LEGACY_MASTER_LABEL in labels
        self._route_reflector_annotation_preexisted = self.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION in annotations
        self._route_reflector_annotation_value = annotations.get(self.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION)

    def _list_bgppeer_names(self):
        result = self._run("kubectl get bgppeers -o json", check=False)
        if result.returncode != 0:
            raise RuntimeError(
                "Could not safely list existing Calico BGPPeer resources; "
                f"refusing to mutate cluster-wide routing state. stderr: {result.stderr.strip()}"
            )
        try:
            peers = json.loads(result.stdout).get("items", [])
        except json.JSONDecodeError as e:
            raise RuntimeError("Could not parse existing Calico BGPPeer resources") from e
        return {peer.get("metadata", {}).get("name") for peer in peers if peer.get("metadata", {}).get("name")}

    def _capture_bgp_peers(self):
        self._original_bgppeer_names = self._list_bgppeer_names()
        if self.BGP_PEER_NAME in self._original_bgppeer_names:
            raise RuntimeError(
                f"Calico BGPPeer/{self.BGP_PEER_NAME} already exists; "
                "refusing to overwrite pre-existing cluster routing policy."
            )

    def _state_configmap_command(self, verb):
        return f"kubectl -n {self._q(self.STATE_NAMESPACE)} {verb} configmap {self._q(self.STATE_CONFIGMAP_NAME)}"

    def _persist_original_state(self):
        self._run(
            f"kubectl create namespace {self._q(self.STATE_NAMESPACE)} --dry-run=client -o yaml | kubectl apply -f -"
        )
        data = {
            self.STATE_CONFIG_PREEXISTED_KEY: json.dumps(bool(self._bgp_config_preexisted)),
            self.STATE_BGP_PEERS_KEY: json.dumps(sorted(self._original_bgppeer_names or [])),
            self.STATE_PRIMARY_NODE_KEY: self.route_reflector_node or "",
            self.STATE_NODE_LABEL_PREEXISTED_KEY: json.dumps(bool(self._legacy_label_preexisted)),
            self.STATE_NODE_ANNOTATION_PREEXISTED_KEY: json.dumps(bool(self._route_reflector_annotation_preexisted)),
        }
        if self.original_bgp_configuration:
            data[self.STATE_CONFIGURATION_KEY] = json.dumps(self.original_bgp_configuration, sort_keys=True)
        if self._route_reflector_annotation_value is not None:
            data[self.STATE_NODE_ANNOTATION_VALUE_KEY] = self._route_reflector_annotation_value

        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.STATE_CONFIGMAP_NAME,
                "namespace": self.STATE_NAMESPACE,
                "labels": {
                    self.PROBLEM_LABEL_KEY: self.PROBLEM_LABEL_VALUE,
                },
            },
            "data": data,
        }
        self._apply_manifest(json.dumps(manifest))

    def _read_persisted_original_state(self):
        result = self._run(self._state_configmap_command("get") + " -o json", check=False)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout).get("data", {}) or {}
        except json.JSONDecodeError:
            return None

    def _restore_persisted_original_state(self):
        data = self._read_persisted_original_state()
        if not data:
            return False

        if self._bgp_config_preexisted is None and self.STATE_CONFIG_PREEXISTED_KEY in data:
            self._bgp_config_preexisted = json.loads(data[self.STATE_CONFIG_PREEXISTED_KEY])
            if self._bgp_config_preexisted and data.get(self.STATE_CONFIGURATION_KEY):
                self.original_bgp_configuration = json.loads(data[self.STATE_CONFIGURATION_KEY])

        if not self.route_reflector_node and data.get(self.STATE_PRIMARY_NODE_KEY):
            self.route_reflector_node = data[self.STATE_PRIMARY_NODE_KEY]

        if self._original_bgppeer_names is None and data.get(self.STATE_BGP_PEERS_KEY):
            self._original_bgppeer_names = set(json.loads(data[self.STATE_BGP_PEERS_KEY]))

        if self._legacy_label_preexisted is None and self.STATE_NODE_LABEL_PREEXISTED_KEY in data:
            self._legacy_label_preexisted = json.loads(data[self.STATE_NODE_LABEL_PREEXISTED_KEY])

        if self._route_reflector_annotation_preexisted is None and self.STATE_NODE_ANNOTATION_PREEXISTED_KEY in data:
            self._route_reflector_annotation_preexisted = json.loads(data[self.STATE_NODE_ANNOTATION_PREEXISTED_KEY])
            self._route_reflector_annotation_value = data.get(self.STATE_NODE_ANNOTATION_VALUE_KEY)

        return True

    def _restart_calico(self):
        self._run("kubectl -n kube-system rollout restart ds/calico-node")
        self._run("kubectl -n kube-system rollout status ds/calico-node --timeout=180s", timeout=210)
        time.sleep(15)

    def _wait_for_deployment_ready(self, name, namespace, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            deployment = self.apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            desired = deployment.spec.replicas or 0
            ready = deployment.status.ready_replicas or 0
            updated = deployment.status.updated_replicas or 0
            unavailable = deployment.status.unavailable_replicas or 0
            if desired > 0 and ready == desired and updated == desired and unavailable == 0:
                return
            time.sleep(2)
        raise TimeoutError(f"Timed out waiting for deployment {namespace}/{name} to become ready")

    def _pin_deployment_to_node(self, name, node_name):
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": {
                            "kubernetes.io/hostname": node_name,
                        }
                    }
                }
            }
        }
        self.apps_v1.patch_namespaced_deployment(name=name, namespace=self.namespace, body=body)
        self._wait_for_deployment_ready(name, self.namespace)

    def _prepare_cross_node_app_path(self):
        self._pin_deployment_to_node("frontend", self.worker_nodes[0])
        self._pin_deployment_to_node("reservation", self.worker_nodes[1])

    def _unpin_deployment_from_node(self, name):
        deployment = self.apps_v1.read_namespaced_deployment(name=name, namespace=self.namespace)
        node_selector = dict(deployment.spec.template.spec.node_selector or {})
        if "kubernetes.io/hostname" not in node_selector:
            return

        patch = json.dumps(
            [
                {
                    "op": "remove",
                    "path": "/spec/template/spec/nodeSelector/kubernetes.io~1hostname",
                }
            ]
        )
        self._run(
            f"kubectl -n {self._q(self.namespace)} patch deployment {self._q(name)} --type=json -p {self._q(patch)}"
        )
        self._wait_for_deployment_ready(name, self.namespace)

    def _restore_app_scheduling(self):
        self._unpin_deployment_from_node("frontend")
        self._unpin_deployment_from_node("reservation")

    def _capture_app_deployment_replicas(self):
        self._app_deployment_replicas = {
            deployment.metadata.name: deployment.spec.replicas or 0
            for deployment in self.apps_v1.list_namespaced_deployment(self.namespace).items
        }

    def _deploy_probe(self):
        self._ensure_probe_namespace_available()
        server_node = self.worker_nodes[1]
        client_node = self.worker_nodes[0]
        self._apply_manifest(f"""apiVersion: v1
kind: Namespace
metadata:
  name: {self.PROBE_NAMESPACE}
  labels:
    {self.PROBLEM_LABEL_KEY}: {self.PROBLEM_LABEL_VALUE}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {self.PROBE_SERVER}
  namespace: {self.PROBE_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {self.PROBE_SERVER}
  template:
    metadata:
      labels:
        app: {self.PROBE_SERVER}
    spec:
      nodeSelector:
        kubernetes.io/hostname: {server_node}
      containers:
      - name: server
        image: busybox:1.36
        imagePullPolicy: IfNotPresent
        command: ["sh", "-c", "mkdir -p /www && echo ok > /www/index.html && httpd -f -p 8080 -h /www"]
        ports:
        - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: {self.PROBE_SERVER}
  namespace: {self.PROBE_NAMESPACE}
spec:
  selector:
    app: {self.PROBE_SERVER}
  ports:
  - port: 8080
    targetPort: 8080
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {self.PROBE_LOCAL_SERVER}
  namespace: {self.PROBE_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {self.PROBE_LOCAL_SERVER}
  template:
    metadata:
      labels:
        app: {self.PROBE_LOCAL_SERVER}
    spec:
      nodeSelector:
        kubernetes.io/hostname: {client_node}
      containers:
      - name: server
        image: busybox:1.36
        imagePullPolicy: IfNotPresent
        command: ["sh", "-c", "mkdir -p /www && echo ok > /www/index.html && httpd -f -p 8080 -h /www"]
        ports:
        - containerPort: 8080
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {self.PROBE_CLIENT}
  namespace: {self.PROBE_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {self.PROBE_CLIENT}
  template:
    metadata:
      labels:
        app: {self.PROBE_CLIENT}
    spec:
      nodeSelector:
        kubernetes.io/hostname: {client_node}
      containers:
      - name: client
        image: busybox:1.36
        imagePullPolicy: IfNotPresent
        command: ["sh", "-c", "sleep 36000"]
""")
        self._run(f"kubectl -n {self.PROBE_NAMESPACE} rollout status deploy/{self.PROBE_SERVER} --timeout=120s")
        self._run(f"kubectl -n {self.PROBE_NAMESPACE} rollout status deploy/{self.PROBE_LOCAL_SERVER} --timeout=120s")
        self._run(f"kubectl -n {self.PROBE_NAMESPACE} rollout status deploy/{self.PROBE_CLIENT} --timeout=120s")

    def _probe_client_pod(self):
        result = self._run(
            f"kubectl -n {self.PROBE_NAMESPACE} get pod -l app={self.PROBE_CLIENT} "
            "-o jsonpath='{.items[0].metadata.name}'",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    def _probe_pod_ip(self, app):
        result = self._run(
            f"kubectl -n {self.PROBE_NAMESPACE} get pod -l app={self._q(app)} -o jsonpath='{{.items[0].status.podIP}}'",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    def _probe_command(self, url):
        client_pod = self._probe_client_pod()
        if client_pod is None:
            return None
        pod = self._q(client_pod)
        return f"kubectl -n {self._q(self.PROBE_NAMESPACE)} exec {pod} -- wget -T 3 -qO- {self._q(url)}"

    def _probe_url_succeeds(self, url):
        command = self._probe_command(url)
        if command is None:
            return False
        result = self._run(command, check=False, timeout=10)
        return result.returncode == 0 and result.stdout.strip() == "ok"

    def _cross_node_service_probe_succeeds(self):
        return self._probe_url_succeeds(f"http://{self.PROBE_SERVER}:8080")

    def _cross_node_pod_ip_probe_succeeds(self):
        pod_ip = self._probe_pod_ip(self.PROBE_SERVER)
        if not pod_ip:
            return False
        return self._probe_url_succeeds(f"http://{pod_ip}:8080")

    def _same_node_pod_ip_probe_succeeds(self):
        pod_ip = self._probe_pod_ip(self.PROBE_LOCAL_SERVER)
        if not pod_ip:
            return False
        return self._probe_url_succeeds(f"http://{pod_ip}:8080")

    def _probe_succeeds(self):
        return (
            self._same_node_pod_ip_probe_succeeds()
            and self._cross_node_pod_ip_probe_succeeds()
            and self._cross_node_service_probe_succeeds()
        )

    def _probe_fault_observed(self):
        return (
            self._same_node_pod_ip_probe_succeeds()
            and not self._cross_node_pod_ip_probe_succeeds()
            and not self._cross_node_service_probe_succeeds()
        )

    def _wait_for_probe(self, expect_success, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready = self._probe_succeeds() if expect_success else self._probe_fault_observed()
            if ready:
                return
            time.sleep(3)
        state = "success" if expect_success else "failure"
        raise TimeoutError(f"Timed out waiting for cross-node probe {state}")

    def _configure_healthy_route_reflectors_with_legacy_label(self):
        rr = self.route_reflector_node
        bgp_config_labels = ""
        if self._bgp_config_preexisted is False:
            bgp_config_labels = f"  labels:\n    {self.PROBLEM_LABEL_KEY}: {self.PROBLEM_LABEL_VALUE}\n"
        self._run(f"kubectl label node {self._q(rr)} {self._q(f'{self.LEGACY_MASTER_LABEL}=')} --overwrite")
        self._run(
            f"kubectl annotate node {self._q(rr)} "
            f"{self._q(f'{self.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION}={self.ROUTE_REFLECTOR_CLUSTER_ID}')} --overwrite"
        )
        self._run(f"kubectl annotate node {self._q(rr)} {self._q(f'{self.NODE_MARKER_ANNOTATION}=true')} --overwrite")
        self._apply_manifest(f"""apiVersion: crd.projectcalico.org/v1
kind: BGPConfiguration
metadata:
  name: default
{bgp_config_labels}spec:
  nodeToNodeMeshEnabled: false
---
apiVersion: crd.projectcalico.org/v1
kind: BGPPeer
metadata:
  name: {self.BGP_PEER_NAME}
  labels:
    {self.PROBLEM_LABEL_KEY}: {self.PROBLEM_LABEL_VALUE}
spec:
  nodeSelector: "!has({self.LEGACY_MASTER_LABEL})"
  peerSelector: "has({self.LEGACY_MASTER_LABEL})"
""")
        self._restart_calico()

    def _remove_legacy_route_reflector_label(self):
        self._run(f"kubectl label node {self._q(self.route_reflector_node)} {self._q(f'{self.LEGACY_MASTER_LABEL}-')}")
        self._restart_calico()

    def _patch_route_reflector_to_current_label(self):
        patch = json.dumps(
            {
                "spec": {
                    "nodeSelector": f"!has({self.CURRENT_CONTROL_PLANE_LABEL})",
                    "peerSelector": f"has({self.CURRENT_CONTROL_PLANE_LABEL})",
                }
            }
        )
        self._run(f"kubectl patch bgppeer {self._q(self.BGP_PEER_NAME)} --type=merge -p {self._q(patch)}")
        self._restart_calico()

    def _resource_has_problem_label(self, command):
        result = self._run(command, check=False)
        if result.returncode != 0:
            return False
        try:
            labels = json.loads(result.stdout).get("metadata", {}).get("labels", {})
        except json.JSONDecodeError:
            return False
        return labels.get(self.PROBLEM_LABEL_KEY) == self.PROBLEM_LABEL_VALUE

    def _problem_created_bgppeer_exists(self):
        return self._resource_has_problem_label(f"kubectl get bgppeer {self._q(self.BGP_PEER_NAME)} -o json")

    def _problem_created_bgp_configuration_exists(self):
        return self._resource_has_problem_label("kubectl get bgpconfiguration default -o json")

    def _probe_namespace_owned_by_problem(self):
        return self._resource_has_problem_label(f"kubectl get namespace {self._q(self.PROBE_NAMESPACE)} -o json")

    def _probe_namespace_exists(self):
        result = self._run(f"kubectl get namespace {self._q(self.PROBE_NAMESPACE)} -o json", check=False)
        return result.returncode == 0

    def _ensure_probe_namespace_available(self):
        if self._probe_namespace_exists() and not self._probe_namespace_owned_by_problem():
            raise RuntimeError(
                f"Namespace/{self.PROBE_NAMESPACE} already exists without {self.PROBLEM_LABEL_KEY}="
                f"{self.PROBLEM_LABEL_VALUE}; refusing to use or delete an unrelated namespace."
            )

    def _delete_probe_namespace_if_owned(self):
        if self._probe_namespace_owned_by_problem():
            self._run(f"kubectl delete namespace {self._q(self.PROBE_NAMESPACE)} --ignore-not-found")

    def _new_bgppeer_names_since_capture(self):
        if self._original_bgppeer_names is None:
            return set()
        return self._list_bgppeer_names() - set(self._original_bgppeer_names)

    def _delete_new_bgppeers_since_capture(self):
        with contextlib.suppress(Exception):
            for name in sorted(self._new_bgppeer_names_since_capture()):
                self._run(f"kubectl delete bgppeer {self._q(name)} --ignore-not-found")

    def _node_has_problem_marker(self, node_name):
        if not hasattr(self, "core_v1"):
            return False
        try:
            node = self.core_v1.read_node(name=node_name)
        except Exception:
            return False
        annotations = node.metadata.annotations or {}
        return annotations.get(self.NODE_MARKER_ANNOTATION) == "true"

    def _route_reflector_nodes_for_cleanup(self):
        if self.route_reflector_node:
            return [self.route_reflector_node]
        if not hasattr(self, "core_v1"):
            return []
        try:
            nodes = self.core_v1.list_node().items
        except Exception:
            return []
        return [
            node.metadata.name
            for node in nodes
            if (node.metadata.annotations or {}).get(self.NODE_MARKER_ANNOTATION) == "true"
        ]

    def _delete_support_resources(self):
        self._restore_persisted_original_state()

        # Calico watches these CRDs and node labels; cleanup avoids another
        # DaemonSet restart because recover_fault already restarts calico-node.
        with contextlib.suppress(Exception):
            self._delete_probe_namespace_if_owned()
        self._delete_new_bgppeers_since_capture()
        should_delete_bgppeer = self._bgp_config_preexisted is not None or self._problem_created_bgppeer_exists()
        if should_delete_bgppeer:
            with contextlib.suppress(Exception):
                self._run(f"kubectl delete bgppeer {self._q(self.BGP_PEER_NAME)} --ignore-not-found")
        if self._bgp_config_preexisted is True and self.original_bgp_configuration:
            with contextlib.suppress(Exception):
                self._apply_manifest(json.dumps(self.original_bgp_configuration))
        elif self._bgp_config_preexisted is False or self._problem_created_bgp_configuration_exists():
            with contextlib.suppress(Exception):
                self._run("kubectl delete bgpconfiguration default --ignore-not-found")

        for node_name in self._route_reflector_nodes_for_cleanup():
            has_problem_marker = self._node_has_problem_marker(node_name)
            if self._legacy_label_preexisted is True:
                with contextlib.suppress(Exception):
                    self._run(
                        f"kubectl label node {self._q(node_name)} {self._q(f'{self.LEGACY_MASTER_LABEL}=')} --overwrite"
                    )
            elif self._legacy_label_preexisted is False or has_problem_marker:
                with contextlib.suppress(Exception):
                    self._run(f"kubectl label node {self._q(node_name)} {self._q(f'{self.LEGACY_MASTER_LABEL}-')}")

            if self._route_reflector_annotation_preexisted is True:
                with contextlib.suppress(Exception):
                    self._run(
                        f"kubectl annotate node {self._q(node_name)} "
                        f"{self._q(f'{self.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION}={self._route_reflector_annotation_value}')} --overwrite"
                    )
            elif self._route_reflector_annotation_preexisted is False or has_problem_marker:
                with contextlib.suppress(Exception):
                    self._run(
                        f"kubectl annotate node {self._q(node_name)} "
                        f"{self._q(f'{self.ROUTE_REFLECTOR_CLUSTER_ID_ANNOTATION}-')}"
                    )
            if (
                has_problem_marker
                or self._legacy_label_preexisted is not None
                or self._route_reflector_annotation_preexisted is not None
            ):
                with contextlib.suppress(Exception):
                    self._run(
                        f"kubectl annotate node {self._q(node_name)} {self._q(f'{self.NODE_MARKER_ANNOTATION}-')}"
                    )
        with contextlib.suppress(Exception):
            self._run(self._state_configmap_command("delete") + " --ignore-not-found")

    def _cleanup(self):
        self._delete_support_resources()
        self._app_cleanup()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        if not self._calico_available():
            raise RuntimeError(
                f"Calico BGP CRDs are required for route-reflector label drift. {self.CLUSTER_REQUIREMENTS}"
            )
        if not self._calico_bgp_dataplane_available():
            raise RuntimeError(
                "Calico BGP dataplane is not available; calico-node must expose birdcl for this "
                f"route-reflector label drift problem. {self.CLUSTER_REQUIREMENTS}"
            )
        self._select_nodes()
        self._delete_support_resources()
        self._capture_bgp_configuration()
        self._capture_bgp_peers()
        self._capture_route_reflector_node_state()
        self._persist_original_state()
        self._capture_app_deployment_replicas()

        print("Preparing cross-node Hotel Reservation path")
        self._prepare_cross_node_app_path()

        print("Deploying cross-node network probe")
        self._deploy_probe()

        print("Configuring healthy Calico route-reflector topology with legacy master label")
        self._configure_healthy_route_reflectors_with_legacy_label()
        self._wait_for_probe(expect_success=True, timeout=120)

        print(f"Removing legacy label '{self.LEGACY_MASTER_LABEL}' from route-reflector node")
        self._remove_legacy_route_reflector_label()
        self._wait_for_probe(expect_success=False, timeout=120)
        print("Calico route-reflector label drift injected.\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        if not self.route_reflector_node:
            self._select_nodes()
        self._patch_route_reflector_to_current_label()
        self._wait_for_probe(expect_success=True, timeout=180)
        self._restore_app_scheduling()
        self.kubectl.wait_for_ready(self.namespace)
        print("Recovered Calico route-reflector selection with current control-plane label.\n")
