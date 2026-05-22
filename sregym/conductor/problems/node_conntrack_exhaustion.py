import contextlib
import textwrap
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.conntrack_mitigation import ConntrackMitigationOracle, read_node_conntrack_usage
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class NodeConntrackExhaustionHotelReservation(Problem):
    gateway_deployment = gateway_service = "rpc-gateway"
    client_deployment = "edge-traffic-client"
    gateway_port, gateway_replicas, client_replicas, connections_per_client = 9090, 4, 27, 10000
    inject_ratio_threshold, recovery_ratio_threshold = 0.98, 0.10
    max_client_replicas = 80

    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.victim_node = self.gateway_node = None
        self.root_cause = self.build_structured_root_cause(
            component="victim node nf_conntrack table + deployment/edge-traffic-client + deployment/rpc-gateway",
            namespace=self.namespace,
            description=(
                "The edge-traffic-client deployment is pinned to one worker node and opens many held TCP connections "
                "to the rpc-gateway service/deployment. Those connections saturate the victim node's Linux "
                "nf_conntrack table, so new connections from that node time out even though Kubernetes Deployments, Pods, Services, and Endpoints still look healthy."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = ConntrackMitigationOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._delete_support_resources()
        self.victim_node, self.gateway_node = self.select_worker_nodes()
        print(f"Victim node: {self.victim_node} | Gateway node: {self.gateway_node}")
        self._calibrate_client_replicas()
        self.core_v1.create_namespaced_service(self.namespace, self._gateway_service())
        self._create_deployment(
            self.gateway_deployment, self.gateway_replicas, self.gateway_node, self._gateway_container()
        )
        self._wait_for_deployment(self.gateway_deployment, self.gateway_replicas)
        self._create_deployment(
            self.client_deployment, self.client_replicas, self.victim_node, self._client_container()
        )
        self._wait_for_deployment(self.client_deployment, self.client_replicas)
        self._wait_for_conntrack(self.victim_node, self.inject_ratio_threshold, timeout=300)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._delete_support_resources()
        node = self.victim_node
        if not node:
            with contextlib.suppress(Exception):
                node = self.select_worker_nodes()[0]
        if node:
            self._wait_for_conntrack(node, self.recovery_ratio_threshold, timeout=180, below=True)

    def select_worker_nodes(self) -> tuple[str, str]:
        control_plane_labels = {"node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"}
        workers = sorted(
            node.metadata.name
            for node in self.kubectl.list_nodes().items
            if not control_plane_labels & set((node.metadata.labels or {}).keys())
        )
        if len(workers) < 2:
            raise RuntimeError("node_conntrack_exhaustion_hotel_reservation requires at least two worker nodes")
        frontend_node = self._frontend_node()
        if frontend_node in workers:
            return frontend_node, next(node for node in workers if node != frontend_node)
        return workers[-1], workers[0]

    def _calibrate_client_replicas(self):
        _, maximum = read_node_conntrack_usage(self.kubectl, self.victim_node, self.namespace)
        target_connections = (maximum * 105 + 99) // 100
        replicas = max(1, (target_connections + self.connections_per_client - 1) // self.connections_per_client)
        if replicas > self.max_client_replicas:
            raise RuntimeError(
                "node_conntrack_exhaustion_hotel_reservation requires "
                f"{replicas} client replicas for nf_conntrack_max={maximum}, "
                f"above cap {self.max_client_replicas}"
            )
        self.client_replicas = replicas
        print(
            f"Calibrated {self.client_deployment} replicas: {replicas} "
            f"(nf_conntrack_max={maximum}, target_connections={target_connections})"
        )

    def _frontend_node(self):
        pods = self.core_v1.list_namespaced_pod(self.namespace, label_selector="io.kompose.service=frontend").items
        for pod in pods:
            if pod.status.phase == "Running" and pod.spec.node_name:
                return pod.spec.node_name

    def _gateway_service(self):
        return {
            "metadata": {"name": self.gateway_service},
            "spec": {"selector": {"app": self.gateway_deployment}, "ports": [{"port": self.gateway_port}]},
        }

    def _create_deployment(self, name: str, replicas: int, node: str, container: dict):
        self.apps_v1.create_namespaced_deployment(self.namespace, self._deployment(name, replicas, node, container))

    def _deployment(self, name: str, replicas: int, node: str, container: dict):
        spec = {
            "nodeName": node,
            "terminationGracePeriodSeconds": 0,
            "automountServiceAccountToken": False,
            "containers": [container],
        }
        if name == self.client_deployment:
            spec["volumes"] = [{"name": "host-proc", "hostPath": {"path": "/proc", "type": "Directory"}}]
        return {
            "metadata": {"name": name, "labels": {"app": name}},
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": name}},
                "template": {"metadata": {"labels": {"app": name}}, "spec": spec},
            },
        }

    def _gateway_container(self):
        script = textwrap.dedent(
            """\
            import socket
            s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 9090)); s.listen(4096)
            held = []
            while True:
                c, _ = s.accept(); held.append(c)
            """
        )
        return {
            "name": "gateway",
            "image": "python:3.12-alpine",
            "command": ["python", "-c", script],
            "ports": [{"containerPort": self.gateway_port}],
        }

    def _client_container(self):
        script = textwrap.dedent(
            """\
            import os, socket, time
            target, port = os.environ["TARGET_HOST"], int(os.environ["TARGET_PORT"])
            addr = socket.getaddrinfo(target, port, type=socket.SOCK_STREAM)[0][4]
            goal, batch = int(os.environ["CONNECTIONS"]), 200
            held = []
            while len(held) < goal:
                for _ in range(min(batch, goal - len(held))):
                    try:
                        s = socket.socket(); s.setblocking(False); s.connect_ex(addr); held.append(s)
                    except OSError: time.sleep(0.02)
                count = open("/host-proc/sys/net/netfilter/nf_conntrack_count").read().strip()
                maximum = open("/host-proc/sys/net/netfilter/nf_conntrack_max").read().strip()
                print(f"held={len(held)} nf_conntrack_count={count} nf_conntrack_max={maximum}", flush=True)
                time.sleep(0.2)
            while True:
                time.sleep(30)
            """
        )
        return {
            "name": "client",
            "image": "python:3.12-alpine",
            "command": ["python", "-c", script],
            "env": [
                {"name": "TARGET_HOST", "value": f"{self.gateway_service}.{self.namespace}.svc.cluster.local"},
                {"name": "TARGET_PORT", "value": str(self.gateway_port)},
                {"name": "CONNECTIONS", "value": str(self.connections_per_client)},
            ],
            "volumeMounts": [{"name": "host-proc", "mountPath": "/host-proc", "readOnly": True}],
        }

    def _wait_for_deployment(self, name: str, replicas: int, timeout: int = 180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.apps_v1.read_namespaced_deployment(name, self.namespace).status
            if (status.available_replicas or 0) >= replicas:
                return
            time.sleep(2)
        raise RuntimeError(f"Deployment {name} did not become ready")

    def _wait_for_conntrack(self, node: str, threshold: float, timeout: int, below: bool = False):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            count, maximum = read_node_conntrack_usage(self.kubectl, node, self.namespace)
            ratio = count / maximum if maximum else 0
            last = f"{count}/{maximum} ({ratio:.2%})"
            print(f"Node {node} conntrack usage: {last}")
            target_reached = ratio <= threshold if below else ratio >= threshold
            if target_reached:
                return
            time.sleep(5)
        raise RuntimeError(f"Conntrack usage on {node} did not reach target threshold: {last}")

    def _delete_support_resources(self):
        for name in (self.client_deployment, self.gateway_deployment):
            with self._ignore_not_found():
                self.apps_v1.delete_namespaced_deployment(name, self.namespace, grace_period_seconds=0)
        with self._ignore_not_found():
            self.core_v1.delete_namespaced_service(self.gateway_service, self.namespace)

    @contextlib.contextmanager
    def _ignore_not_found(self):
        try:
            yield
        except ApiException as e:
            if e.status != 404:
                raise
