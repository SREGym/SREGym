import contextlib
import textwrap
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.conntrack_mitigation import (
    ConntrackMitigationOracle,
    read_node_conntrack_usage,
    write_node_conntrack_max,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class NodeConntrackExhaustionHotelReservation(Problem):
    gateway_deployment = gateway_service = "rpc-gateway"
    client_deployment = "edge-traffic-client"
    gateway_port, gateway_port_count, gateway_replicas = 9090, 16, 4
    client_replicas, connections_per_worker = 1, 10000
    conntrack_max_cap = 262144
    inject_ratio_threshold, recovery_ratio_threshold = 0.98, 0.10

    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.victim_node = self.gateway_node = None
        self.original_conntrack_max = self.target_connections = None
        self.conntrack_max_changed = False
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
        self.victim_node, _ = self.select_worker_nodes()
        self.gateway_node = self.victim_node
        print(f"Victim node: {self.victim_node} | Gateway node: {self.gateway_node}")
        try:
            self._prepare_conntrack_limit()
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
        except Exception:
            with contextlib.suppress(Exception):
                self._delete_support_resources()
            self._restore_conntrack_limit()
            raise

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        try:
            self._delete_support_resources()
            node = self.victim_node
            if not node:
                with contextlib.suppress(Exception):
                    node = self.select_worker_nodes()[0]
            if node:
                self._wait_for_conntrack(node, self.recovery_ratio_threshold, timeout=180, below=True)
        finally:
            self._restore_conntrack_limit()

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

    def _prepare_conntrack_limit(self):
        node = self.victim_node
        if not node:
            raise RuntimeError("Cannot prepare nf_conntrack_max before selecting a victim node")
        count, original_maximum = read_node_conntrack_usage(self.kubectl, node, self.namespace)
        self.original_conntrack_max = original_maximum
        effective_maximum = min(original_maximum, self.conntrack_max_cap)
        if effective_maximum < original_maximum:
            if count >= effective_maximum * self.inject_ratio_threshold:
                raise RuntimeError(
                    f"Refusing to lower nf_conntrack_max on {node}: "
                    f"current usage {count} is already above {self.inject_ratio_threshold:.0%} "
                    f"of injection limit {effective_maximum}"
                )
            self.conntrack_max_changed = True
            write_node_conntrack_max(self.kubectl, node, effective_maximum, self.namespace)
            _, updated_maximum = read_node_conntrack_usage(self.kubectl, node, self.namespace)
            if updated_maximum != effective_maximum:
                raise RuntimeError(
                    f"Could not set nf_conntrack_max on {node} to {effective_maximum}; observed {updated_maximum}"
                )

        self.target_connections = (effective_maximum * 105 + 99) // 100
        print(
            f"Calibrated {self.client_deployment}: {self.client_replicas} pod "
            f"(nf_conntrack_max={effective_maximum}, target_connections={self.target_connections})"
        )

    def _restore_conntrack_limit(self):
        if not self.conntrack_max_changed or not self.victim_node or self.original_conntrack_max is None:
            return
        write_node_conntrack_max(self.kubectl, self.victim_node, self.original_conntrack_max, self.namespace)
        _, maximum = read_node_conntrack_usage(self.kubectl, self.victim_node, self.namespace)
        if maximum != self.original_conntrack_max:
            raise RuntimeError(
                f"Could not restore nf_conntrack_max on {self.victim_node} "
                f"to {self.original_conntrack_max}; observed {maximum}"
            )
        self.conntrack_max_changed = False

    def _frontend_node(self):
        pods = self.core_v1.list_namespaced_pod(self.namespace, label_selector="io.kompose.service=frontend").items
        for pod in pods:
            if pod.status.phase == "Running" and pod.spec.node_name:
                return pod.spec.node_name

    def _gateway_service(self):
        return {
            "metadata": {"name": self.gateway_service},
            "spec": {
                "selector": {"app": self.gateway_deployment},
                "ports": [{"name": f"tcp-{port}", "port": port, "targetPort": port} for port in self._gateway_ports()],
            },
        }

    def _gateway_ports(self):
        return list(range(self.gateway_port, self.gateway_port + self.gateway_port_count))

    def _create_deployment(self, name: str, replicas: int, node: str, container: dict):
        self.apps_v1.create_namespaced_deployment(self.namespace, self._deployment(name, replicas, node, container))

    def _deployment(self, name: str, replicas: int, node: str, container: dict):
        spec = {
            "nodeName": node,
            "terminationGracePeriodSeconds": 0,
            "automountServiceAccountToken": False,
            "containers": [container],
        }
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
            import os, select, socket
            ports = [int(port) for port in os.environ["PORTS"].split(",")]
            listeners = []
            for port in ports:
                s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port)); s.listen(4096)
                listeners.append(s)
            held = []
            while True:
                ready, _, _ = select.select(listeners, [], [])
                for listener in ready:
                    c, _ = listener.accept(); held.append(c)
            """
        )
        return {
            "name": "gateway",
            "image": "python:3.12-alpine",
            "command": ["sh", "-c", 'ulimit -n 524288; exec python -c "$SCRIPT"'],
            "env": [
                {"name": "PORTS", "value": ",".join(str(port) for port in self._gateway_ports())},
                {"name": "SCRIPT", "value": script},
            ],
            "ports": [{"containerPort": port} for port in self._gateway_ports()],
        }

    def _client_container(self):
        script = textwrap.dedent(
            """\
            import multiprocessing as mp, os, socket, time
            target = os.environ["TARGET_HOST"]
            ports = [int(port) for port in os.environ["TARGET_PORTS"].split(",")]
            total = int(os.environ["CONNECTIONS"])
            per_worker = int(os.environ["CONNECTIONS_PER_WORKER"])
            def hold_connections(goal):
                addrs = [socket.getaddrinfo(target, port, type=socket.SOCK_STREAM)[0][4] for port in ports]
                held = []
                while len(held) < goal:
                    for _ in range(min(200, goal - len(held))):
                        try:
                            s = socket.socket(); s.setblocking(False)
                            s.connect_ex(addrs[len(held) % len(addrs)])
                            held.append(s)
                        except OSError:
                            time.sleep(0.02)
                    time.sleep(0.2)
                while True:
                    time.sleep(30)
            workers, remaining = [], total
            while remaining:
                goal = min(per_worker, remaining)
                worker = mp.Process(target=hold_connections, args=(goal,))
                worker.start(); workers.append(worker)
                remaining -= goal
            while all(worker.is_alive() for worker in workers):
                time.sleep(2)
            raise RuntimeError("client connection worker stopped")
            """
        )
        return {
            "name": "client",
            "image": "python:3.12-alpine",
            "command": ["sh", "-c", 'ulimit -n 524288; exec python -c "$SCRIPT"'],
            "env": [
                {"name": "TARGET_HOST", "value": f"{self.gateway_service}.{self.namespace}.svc.cluster.local."},
                {"name": "TARGET_PORTS", "value": ",".join(str(port) for port in self._gateway_ports())},
                {"name": "CONNECTIONS", "value": str(self.target_connections)},
                {"name": "CONNECTIONS_PER_WORKER", "value": str(self.connections_per_worker)},
                {"name": "SCRIPT", "value": script},
            ],
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
