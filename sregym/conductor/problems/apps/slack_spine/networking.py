"""Slack Spine: networking."""

from __future__ import annotations

import json
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.calico_route_reflector_mitigation import CalicoRouteReflectorMitigationOracle
from sregym.conductor.oracles.ingress_misroute_oracle import IngressMisrouteMitigationOracle
from sregym.conductor.oracles.internal_traffic_policy_mitigation import InternalTrafficPolicyMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.network_policy_oracle import NetworkPolicyMitigationOracle
from sregym.conductor.problems.apps.slack_spine.helpers import APP, delete_pods, wait_rollout
from sregym.conductor.problems.calico_route_reflector_label_drift import CalicoRouteReflectorLabelDriftHotelReservation
from sregym.conductor.problems.environment import (
    configure_port,
    make_app,
    reopen_connections,
)
from sregym.conductor.problems.ingress_misroute import IngressMisroute
from sregym.conductor.problems.internal_traffic_policy_local import InternalTrafficPolicyLocalAstronomyShop
from sregym.conductor.problems.network_policy_block import NetworkPolicyBlock
from sregym.conductor.problems.pod_cidr_exhaustion_hotel_reservation import PodCIDRExhaustionHotelReservation
from sregym.observer.ingress_nginx import IngressNginx
from sregym.utils.decorators import mark_fault_injected


class PodCIDRExhaustionSlack(PodCIDRExhaustionHotelReservation):
    """Calico IPAM exhaustion: the default pool disabled, a tiny strict-affinity pool consumed.

    Only svc-message's pod is replaced (the original deleted every app pod):
    its replacement stays in ContainerCreating without an IP, so history reads
    and sends fail while the rest of Slack Spine keeps its addresses.
    """

    # Enough pause pods that the worker owning the tiny pool's only /26 block
    # fills it whatever the scheduler's spread (3 workers x ~70 pods).
    NUM_EXHAUST_PODS = 210
    BLOCK_SIZE = 64

    def __init__(self, faulty_service: str = "svc-message"):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component="cluster-networking",
            description=(
                "Calico IPAM IP exhaustion. The default IPPool `default-ipv4-ippool` is disabled, the only enabled "
                f"pool is the tiny `{self.TINY_POOL_NAME}` ({self.TINY_POOL_CIDR}, one /26 block) and "
                "`strictAffinity` is enabled in IPAMConfig `default`, so nodes cannot borrow blocks from each other. "
                f"The Deployment `{self.EXHAUST_DEPLOYMENT}` in namespace `{self.EXHAUST_NAMESPACE}` "
                f"({self.NUM_EXHAUST_PODS} pause pods) has consumed every address of that block. The `{faulty_service}` "
                "pod was rescheduled and its replacement is stuck in ContainerCreating with `failed to request IPv4 "
                "addresses: Assigned 0 out of 1 requested IPv4 addresses; No more free affine blocks and strict "
                "affinity enabled`, so message history and sends fail although the Deployment spec is healthy. "
                "Mitigation: free IP allocations by scaling down the consuming workload, or restore the default IP "
                "pool (re-enable it / disable strictAffinity)."
            ),
            oracle_factory=MitigationOracle,
        )

    def _running_exhaust_pods(self) -> int:
        out = self.kubectl.exec_command(f"kubectl get pods -n {self.EXHAUST_NAMESPACE} --no-headers") or ""
        return sum(1 for line in out.splitlines() if " Running " in line)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.kubectl.exec_command(
            'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":true}}\''
        )
        self._apply_manifest(f"""apiVersion: crd.projectcalico.org/v1
kind: IPPool
metadata:
  name: {self.TINY_POOL_NAME}
spec:
  cidr: {self.TINY_POOL_CIDR}
  ipipMode: Always
  natOutgoing: true
  disabled: false
""")
        self.kubectl.exec_command(
            f'kubectl patch ippool {self.DEFAULT_POOL_NAME} --type=merge -p \'{{"spec":{{"disabled":true}}}}\''
        )
        self.kubectl.exec_command(
            f"kubectl create namespace {self.EXHAUST_NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -"
        )
        self._apply_manifest(f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {self.EXHAUST_DEPLOYMENT}
  namespace: {self.EXHAUST_NAMESPACE}
spec:
  replicas: {self.NUM_EXHAUST_PODS}
  selector:
    matchLabels:
      app: batch-worker
  template:
    metadata:
      labels:
        app: batch-worker
        workload-type: batch
        priority: low
        team: data-engineering
    spec:
      containers:
      - name: worker
        image: registry.k8s.io/pause:3.9
        resources:
          requests:
            cpu: "1m"
            memory: "1Mi"
""")
        # The block is full once a block's worth of pause pods run (the
        # others wait without an address).
        deadline = time.monotonic() + 300
        running = 0
        while time.monotonic() < deadline:
            running = self._running_exhaust_pods()
            if running >= self.BLOCK_SIZE - 2:
                break
            time.sleep(5)
        print(f"{running} pause pods hold addresses from {self.TINY_POOL_NAME}")
        time.sleep(10)
        delete_pods(self, self.faulty_service)
        print(f"IP pool exhausted; {self.faulty_service} cannot get a pod IP")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.kubectl.exec_command(
            f"kubectl scale deployment {self.EXHAUST_DEPLOYMENT} -n {self.EXHAUST_NAMESPACE} --replicas=0"
        )
        self.kubectl.exec_command(
            f'kubectl patch ippool {self.DEFAULT_POOL_NAME} --type=merge -p \'{{"spec":{{"disabled":false}}}}\''
        )
        self.kubectl.exec_command(f"kubectl delete ippool {self.TINY_POOL_NAME} --ignore-not-found")
        self.kubectl.exec_command(
            'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":false}}\''
        )
        self.kubectl.exec_command(f"kubectl delete namespace {self.EXHAUST_NAMESPACE} --ignore-not-found --wait=false")
        # Pods stuck without an address back off between CNI retries; replace them.
        out = self.kubectl.exec_command(f"kubectl get pods -n {self.namespace} --no-headers") or ""
        for line in out.splitlines():
            if "ContainerCreating" in line:
                self.kubectl.exec_command(f"kubectl delete pod {line.split()[0]} -n {self.namespace} --wait=false")
        wait_rollout(self, self.faulty_service, timeout_s=300)
        print("Recovery complete")


class CalicoRouteReflectorLabelDriftSlack(CalicoRouteReflectorLabelDriftHotelReservation):
    """Calico route reflector selected by the deprecated master label, which an upgrade removed.

    svc-message and svc-auth are pinned to different workers first (the
    original pinned frontend and reservation), so the send path's session
    check crosses nodes; with node-to-node mesh off and no reflector
    selected, cross-node pod traffic stops.
    """

    CROSS_NODE_PAIR = ("svc-message", "svc-auth")

    def __init__(self):
        super().__init__(app=make_app(APP))
        self.faulty_service = None
        self.frontend_probe_path = "/healthz"
        self.route_reflector_node = None
        self.worker_nodes = []
        self.original_bgp_configuration = None
        self._original_bgppeer_names = None
        self._bgp_config_preexisted = None
        self._legacy_label_preexisted = None
        self._route_reflector_annotation_preexisted = None
        self._route_reflector_annotation_value = None
        self._app_deployment_replicas = {}
        configure_port(
            self,
            APP,
            component=f"BGPPeer/{self.BGP_PEER_NAME}",
            description=(
                "Calico runs in route-reflector mode with node-to-node mesh disabled (BGPConfiguration `default`, "
                f"`nodeToNodeMeshEnabled: false`). The BGPPeer `{self.BGP_PEER_NAME}` selects route reflectors with "
                f"the deprecated `{self.LEGACY_MASTER_LABEL}` label, but after a label migration the intended "
                f"control-plane route-reflector node only carries `{self.CURRENT_CONTROL_PLANE_LABEL}`. No node is "
                "selected as route reflector, so Calico stops propagating routes between nodes and cross-node pod "
                "and Service traffic is dropped. Slack Spine's pods stay mostly Running, but every cross-node hop "
                "fails: svc-message (pinned to one worker) cannot reach svc-auth (pinned to another), the database "
                "or its other dependencies on other nodes, and clients on other nodes cannot reach svc-message, so "
                "history reads and sends fail or time out. Mitigation must repair the BGPPeer selector (or "
                "intentionally restore the expected route-reflector label) while preserving the route-reflector "
                "topology (node-to-node mesh stays disabled)."
            ),
            oracle_factory=CalicoRouteReflectorMitigationOracle,
        )
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self._app_cleanup = self.app.cleanup
        self.app.cleanup = self._cleanup

    def _prepare_cross_node_app_path(self):
        for deployment, node in zip(self.CROSS_NODE_PAIR, self.worker_nodes, strict=False):
            self._pin_deployment_to_node(deployment, node)

    def _restore_app_scheduling(self):
        for deployment in self.CROSS_NODE_PAIR:
            self._unpin_deployment_from_node(deployment)


class IngressMisrouteSlack(IngressMisroute):
    """An ingress-nginx Ingress in front of svc-message has its ``/api`` rule pointed at svc-thread.

    The load generator's message traffic (history reads, posts, edits,
    reactions) reaches svc-message through the ingress controller (deploy
    time: ``loadgen.target`` is the controller's ``/api`` prefix); the other
    roles it calls directly. The fault repoints the path's backend.
    """

    CONTROLLER_URL = "http://ingress-nginx-controller.ingress-nginx.svc.cluster.local"
    PATH = "/api"

    def __init__(self, correct_service: str = "svc-message", wrong_service: str = "svc-thread"):
        super().__init__(app=make_app(APP))
        self.path = self.PATH
        self.correct_service = correct_service
        self.wrong_service = wrong_service
        self.ingress_name = "slack-spine-ingress"
        self.faulty_service = [correct_service, wrong_service]
        configure_port(
            self,
            APP,
            component=self.ingress_name,
            description=(
                f"Ingress `{self.ingress_name}` (ingress-nginx, the edge that serves the users' message API under "
                f"`{self.path}`) has a misconfigured backend rule for path `{self.path}(/|$)(.*)`: it routes "
                f"requests to Service `{wrong_service}` instead of `{correct_service}`. Traffic reaches a valid, "
                f"healthy service, but `{wrong_service}` does not serve the message API, so history reads "
                "(`GET /channels/:id/messages`), sends (`POST /messages`), edits and reactions return 404 through "
                "the edge, while direct calls to svc-message and every pod stay healthy. Mitigation: point the "
                f"`{self.path}` rule back at `{correct_service}`."
            ),
            oracle_factory=IngressMisrouteMitigationOracle,
        )
        self.networking_v1 = client.NetworkingV1Api()
        base = {
            f"LOADGEN_{key}_BASE_URL": f"http://svc-{role}:8000"
            for key, role in (("THREAD", "thread"), ("NOTIF", "notification"), ("SEARCH", "search"))
        }
        self.app.configure(
            {
                "loadgen": {"target": self.CONTROLLER_URL + self.path},
                "sregym": {
                    "loadgenEnv": [
                        {"name": "EPISODE_START_TIMEOUT_S", "value": "86400"},
                        *({"name": k, "value": v} for k, v in base.items()),
                    ]
                },
            }
        )
        # The edge must exist before the load generator starts its episode.
        self._app_deploy = self.app.deploy
        self.app.deploy = self._deploy_with_edge

    @property
    def ingress_probe_path(self) -> str:
        return f"{self.path}/channels/chan-0/messages?limit=1"

    @staticmethod
    def ingress_response_valid(payload) -> bool:
        # svc-thread also exposes /healthz, but does not serve message history.
        return isinstance(payload, dict) and isinstance(payload.get("messages"), list)

    def _ingress(self, backend: str) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": self.ingress_name,
                "namespace": self.namespace,
                "annotations": {"nginx.ingress.kubernetes.io/rewrite-target": "/$2"},
            },
            "spec": {
                "ingressClassName": "nginx",
                "rules": [
                    {
                        "http": {
                            "paths": [
                                {
                                    "path": self.path + "(/|$)(.*)",
                                    "pathType": "ImplementationSpecific",
                                    "backend": {"service": {"name": backend, "port": {"number": 8000}}},
                                }
                            ]
                        }
                    }
                ],
            },
        }

    def _install_edge(self) -> None:
        # A previous attempt's release fails `helm upgrade --install` on the next
        # attempt (metadata.managedFields must be nil): start every attempt clean.
        edge = IngressNginx()
        self.kubectl.exec_command(f"helm uninstall {edge.release_name} -n {edge.namespace} --wait --ignore-not-found")
        edge.deploy()
        self.kubectl.create_namespace_if_not_exist(self.namespace)
        self.kubectl.exec_command_checked(
            f"kubectl apply -n {self.namespace} -f -", input_data=json.dumps(self._ingress(self.correct_service))
        )

    def _ensure_edge(self) -> None:
        self._install_edge()
        # Wait until the controller serves the route.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status, _ = self.app.http(f"{self.CONTROLLER_URL}{self.path}/healthz", timeout=5)
            if status == 200:
                return
            time.sleep(3)
        raise RuntimeError("ingress-nginx did not route /api/healthz to svc-message")

    def _deploy_with_edge(self):
        # The load generator's init container waits for its target, so the
        # controller and the Ingress exist before the chart is installed.
        self._install_edge()
        self._app_deploy()
        self._ensure_edge()

    def _set_backend(self, backend: str) -> None:
        ingress = self.networking_v1.read_namespaced_ingress(name=self.ingress_name, namespace=self.namespace)
        for rule in ingress.spec.rules:
            for path in rule.http.paths:
                if path.path.startswith(self.path):
                    path.backend.service.name = backend
        self.networking_v1.replace_namespaced_ingress(name=self.ingress_name, namespace=self.namespace, body=ingress)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        try:
            self.networking_v1.read_namespaced_ingress(name=self.ingress_name, namespace=self.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self._ensure_edge()
        self._set_backend(self.wrong_service)
        print(f"Ingress {self.ingress_name}: {self.path} -> {self.wrong_service}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._set_backend(self.correct_service)


class NetworkPolicyBlockIA(NetworkPolicyBlock):
    def __init__(
        self, app_name: str = "slack_spine", faulty_service: str = "svc-auth", client_deployment: str = "svc-message"
    ):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.client_deployment = client_deployment
        self.policy_name = f"deny-all-{faulty_service}"
        # Probe from the frontend's network identity: it must reach the target
        # and the frontend must still answer its readiness endpoint.
        self.probe_path = "/healthz"
        self.probe_expect = ""
        configure_port(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"A NetworkPolicy `{self.policy_name}` blocks all ingress and egress traffic for the pods of "
                f"deployment `{faulty_service}`, isolating it from the network. Calls to and from it fail, so "
                f"every request path that depends on it breaks (logins, and `{client_deployment}`'s session check "
                "on every message send) even though its pods remain Running and Ready."
            ),
            oracle_factory=NetworkPolicyMitigationOracle,
        )
        self.networking_v1 = client.NetworkingV1Api()

    @mark_fault_injected
    def inject_fault(self):
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": self.policy_name, "namespace": self.namespace},
            "spec": {
                "podSelector": {"matchLabels": dict(deployment.spec.selector.match_labels)},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }
        self.networking_v1.create_namespaced_network_policy(namespace=self.namespace, body=policy)
        # Established connections survive the policy (and a restarted svc-auth pod could not even reach
        # its database), so roll the client that checks sessions on every send.
        reopen_connections(self, self.client_deployment)


class InternalTrafficPolicyLocalIA(InternalTrafficPolicyLocalAstronomyShop):
    """``internalTrafficPolicy: Local`` on Slack Spine's notification Service, its caller on another node."""

    FAULTY_SERVICE = "svc-notification"
    SERVICE_PORT = 8000
    POD_LABEL_SELECTOR = "app.kubernetes.io/component=svc-notification"
    CALLER_SERVICE = "svc-message"
    CALLER_POD_LABEL_SELECTOR = "app.kubernetes.io/component=svc-message"

    def __init__(self, app_name: str = "slack_spine"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = self.FAULTY_SERVICE
        self.pod_node: str | None = None
        self.victim_node: str | None = None
        configure_port(
            self,
            app_name,
            component=f"service/{self.FAULTY_SERVICE}",
            description=(
                f"The `{self.FAULTY_SERVICE}` Service has `spec.internalTrafficPolicy: Local`, so kube-proxy "
                "routes in-cluster traffic only to a pod on the caller's own node. The deployment has a single "
                "replica pinned to one worker, while its clients run on other nodes (the users' unread-count "
                "requests, `GET /unread`, and svc-message's notification fan-out), so their connections are "
                "silently dropped and hang until the client times out. The pod is "
                "Running and Ready and the endpoints are populated; the fault is only visible in "
                "`internalTrafficPolicy` and the pod placement. Valid mitigations: set the policy back to "
                "`Cluster` (or remove it), or run a ready pod on every worker node."
            ),
            oracle_factory=InternalTrafficPolicyMitigationOracle,
        )
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

    LOADGEN_POD_LABEL_SELECTOR = "app.kubernetes.io/component=loadgen"

    def _select_nodes(self) -> tuple[str, str]:
        """Pin the backend away from the load generator's node.

        The load generator calls ``svc-notification`` directly (``GET /unread``),
        while svc-message's notify fan-out is fire-and-forget. If the backend
        landed on the load generator's node, ``Local`` would still route its
        traffic and users would see nothing, so the victim node is the load
        generator's own (it is not restarted, keeping its ledger).
        """
        workers = self.worker_nodes()
        if len(workers) < 2:
            raise RuntimeError("internal_traffic_policy_local requires at least two worker nodes")
        loadgen_nodes = sorted(self._nodes_with_running_pod(self.LOADGEN_POD_LABEL_SELECTOR) & set(workers))
        victim_node = loadgen_nodes[0] if loadgen_nodes else workers[1]
        pod_node = next(node for node in workers if node != victim_node)
        return pod_node, victim_node
