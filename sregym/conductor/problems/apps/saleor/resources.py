"""Saleor: resources."""

from __future__ import annotations

import copy
import json
import shlex
import time

from kubernetes import client

from sregym.conductor.oracles.apps.saleor.resources import (
    SaleorFDMitigationOracle,
    StatefulSetMemoryLimitMitigationOracle,
)
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.nightly_rebalance_oom_mitigation import NightlyRebalanceOOMMitigationOracle
from sregym.conductor.oracles.priority_preemption_mitigation import PriorityPreemptionMitigationOracle
from sregym.conductor.problems.apps.saleor.helpers import (
    API,
    API_SERVICE,
    APP,
    HIDDEN_NAMESPACE,
    apply_manifest,
    ensure_namespace,
    state_file,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.conductor.problems.file_descriptor_exhaustion import FileDescriptorExhaustion
from sregym.conductor.problems.namespace_memory_limit import NamespaceMemoryLimit
from sregym.conductor.problems.nightly_rebalance_oom import NightlyRebalanceOOM
from sregym.conductor.problems.priority_preemption_cascade import PriorityPreemptionCascadeHotelReservation
from sregym.conductor.problems.resource_request import ResourceRequest
from sregym.utils.decorators import mark_fault_injected


class ResourceRequestTooSmallSaleor(ResourceRequest):
    """On saleor-api: the load generator never waits on Celery, so a worker outage is invisible to users."""

    def __init__(self, faulty_service: str = API):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component=faulty_service,
            description=(
                f"Deployment `{faulty_service}` has an extremely low memory limit (`10Mi`, with the request lowered "
                "to match), far below what the Python/Django process needs to start, so the container is OOMKilled "
                "on every start and the pod crash-loops (CrashLoopBackOff, last state OOMKilled) without becoming "
                f"Ready; Service `{API_SERVICE}` loses its endpoint and GraphQL API requests fail."
            ),
            oracle_factory=MitigationOracle,
        )

    def set_memory_limit(self, deployment_yaml):
        dyaml = copy.deepcopy(deployment_yaml)
        resources = dyaml["spec"]["template"]["spec"]["containers"][0].setdefault("resources", {})
        resources.setdefault("limits", {})["memory"] = "10Mi"
        # The chart's memory request is larger than 10Mi, which the API server would reject.
        resources.setdefault("requests", {})["memory"] = "10Mi"
        print(f"Setting memory limit and request to 10Mi for {self.faulty_service}")
        return dyaml


class NightlyRebalanceOOMSaleor(NightlyRebalanceOOM):
    """On saleor-api: the load generator never waits on Celery, so a worker outage is invisible to users."""

    app_namespace = "saleor"
    # bitnami/kubectl is no longer pulled freely from Docker Hub.
    actor_image = "alpine/k8s:1.28.3"

    def __init__(self, faulty_service: str = API):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        self._target_container = None
        self._original_memory_limit = None
        self._original_memory_request = None
        configure_port(
            self,
            APP,
            component=f"{self.actor_name} CronJob ({self.actor_namespace})",
            description=(
                f"The fault originates from the scheduled `{self.actor_name}` CronJob in `{self.actor_namespace}` "
                f"(policy ConfigMap `{self.policy_configmap}`), which every minute patches deployment "
                f"`{faulty_service}` (Saleor's GraphQL API) down to a `{self.squeeze_memory}` memory limit and "
                "request. That is below the process's startup working set, so the container is OOMKilled during "
                f"start and stays in CrashLoopBackOff; Service `{API_SERVICE}` has no endpoints and API requests "
                "fail. A diagnosis that only "
                f"names the OOMKilled `{faulty_service}` pod or its memory limit without identifying the recurring "
                f"`{self.actor_name}` actor is incomplete. A durable fix must suspend/remove the `{self.actor_name}` "
                f"CronJob or correct its policy, and restore a sane memory limit on `{faulty_service}`."
            ),
            oracle_factory=NightlyRebalanceOOMMitigationOracle,
        )

    @property
    def target_pod_labels(self) -> dict:
        """Labels of the target's pods (read by NightlyRebalanceOOMMitigationOracle)."""
        return dict(self.kubectl.get_deployment(self.faulty_service, self.namespace).spec.selector.match_labels)

    @mark_fault_injected
    def inject_fault(self):
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        containers = dep.spec.template.spec.containers
        container = next((c for c in containers if c.name == self.faulty_service), containers[0])
        requests = (container.resources.requests or {}) if container.resources else {}
        self._original_memory_request = requests.get("memory")
        super().inject_fault()
        # The squeezed pod must actually replace the healthy one: with a surge rollout the old pod
        # would keep serving behind the crash-looping new one and users would never see the OOM loop.
        self._retire_old_replica_sets()

    def _retire_old_replica_sets(self):
        """Delete the target's pre-squeeze ReplicaSets (and their pods).

        Deleting only the old pod is not enough: the old ReplicaSet recreates it
        while the rollout waits for the crash-looping new pod. Removing the old
        ReplicaSets leaves the Deployment spec untouched (the actor's patch is
        the only change) and the squeezed ReplicaSet as the only one.
        """
        apps = client.AppsV1Api()
        deployment = apps.read_namespaced_deployment(self.faulty_service, self.namespace)
        selector = ",".join(f"{k}={v}" for k, v in deployment.spec.selector.match_labels.items())
        for rs in apps.list_namespaced_replica_set(self.namespace, label_selector=selector).items:
            if not any(ref.uid == deployment.metadata.uid for ref in rs.metadata.owner_references or []):
                continue
            limits = {}
            for c in rs.spec.template.spec.containers:
                if c.name == self._target_container and c.resources and c.resources.limits:
                    limits = c.resources.limits
            if limits.get("memory") != self.squeeze_memory:
                print(f"Deleting pre-squeeze ReplicaSet {rs.metadata.name}")
                apps.delete_namespaced_replica_set(rs.metadata.name, self.namespace, propagation_policy="Background")

    def _squeeze_patch(self) -> dict:
        memory = {"memory": self.squeeze_memory}
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            # The chart's request exceeds the squeezed limit, which the API server rejects.
                            {"name": self._target_container, "resources": {"limits": memory, "requests": memory}}
                        ]
                    }
                }
            }
        }

    def _wait_for_target_unhealthy(self, timeout: int):
        unhealthy_reasons = {"CrashLoopBackOff", "CreateContainerError", "RunContainerError", "StartError"}
        selector = ",".join(f"{k}={v}" for k, v in self.target_pod_labels.items())
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector).items
            for pod in pods:
                for cs in pod.status.container_statuses or []:
                    waiting = cs.state.waiting.reason if cs.state.waiting else None
                    terminated = cs.state.terminated.reason if cs.state.terminated else None
                    last = f"{pod.metadata.name}: waiting={waiting} terminated={terminated} restarts={cs.restart_count}"
                    if waiting in unhealthy_reasons or terminated == "OOMKilled" or (cs.restart_count or 0) >= 1:
                        print(f"Fault confirmed live: {last}")
                        return
            print(f"Waiting for squeeze to take effect... {last}")
            time.sleep(5)
        raise RuntimeError(f"Target {self.faulty_service} did not become unhealthy in time; last={last}")

    def _restore_memory_limit(self):
        if self._target_container is None:
            return
        resources = {"limits": {"memory": self._original_memory_limit}}
        if self._original_memory_request is not None:
            resources["requests"] = {"memory": self._original_memory_request}
        patch = {
            "spec": {"template": {"spec": {"containers": [{"name": self._target_container, "resources": resources}]}}}
        }
        self.kubectl.patch_deployment(self.faulty_service, self.namespace, patch)


class PriorityPreemptionCascadeSaleor(PriorityPreemptionCascadeHotelReservation):
    """The unsafe default PriorityClass lets a tenant workload preempt saleor-api.

    saleor-api and saleor-worker share a node-bound volume, so the node cannot be
    chosen; the API pod is still the deterministic victim because every other
    priority-0 pod on its node (StatefulSet pods, the load generator,
    observability) started earlier, and the scheduler reprieves older pods of
    equal priority first. The load generator is left out of the peer
    protection rollout so its ledger survives.
    """

    UNPROTECTED_PEERS = ("loadgen",)

    def __init__(self, faulty_service: str = API, service_name: str = API_SERVICE):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        # PriorityPreemptionMitigationOracle checks this Service's endpoints.
        self.service_name = service_name
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.scheduling_v1 = client.SchedulingV1Api()
        self.target_node = None
        self.target_request_memory = None
        self.pressure_request_memory = None
        self._priority_class_snapshots = {}
        self._deployment_priority_classes = {}
        self._target_original_resources = None
        self._target_original_node_selector = None
        configure_port(
            self,
            APP,
            component=f"PriorityClass/{self.PLATFORM_PRIORITY_CLASS}",
            description=(
                f"PriorityClass `{self.PLATFORM_PRIORITY_CLASS}` (value 100000) was made the cluster-wide "
                f"`globalDefault`. The production `{faulty_service}` pod (Saleor's GraphQL API) kept priority 0, "
                f"while a new tenant workload `{self.PRESSURE_NAMESPACE}/{self.PRESSURE_DEPLOYMENT}` received the "
                "medium default and a memory request large enough to force scheduler preemption on the API pod's "
                "node. The scheduler evicted the API pod; its replacement inherits the same medium default instead "
                f"of the higher `{self.PRODUCTION_PRIORITY_CLASS}` class, cannot preempt the tenant back and stays "
                f"Pending, so Service `{service_name}` has no endpoints and the API is down even though its image, "
                f"Service and config are valid. Mitigation must make `{self.PLATFORM_PRIORITY_CLASS}` no longer an "
                f"unsafe global default and explicitly protect `{faulty_service}` with a higher-valued production "
                "PriorityClass (without deleting the tenant workload or shrinking requests)."
            ),
            oracle_factory=PriorityPreemptionMitigationOracle,
        )
        self._app_cleanup = self.app.cleanup
        self.app.cleanup = self._cleanup

    def _protect_peer_deployments(self):
        peer_names = [
            deployment.metadata.name
            for deployment in self._app_deployments()
            if deployment.metadata.name not in (self.faulty_service, *self.UNPROTECTED_PEERS)
        ]
        body = {"spec": {"template": {"spec": {"priorityClassName": self.PLATFORM_PRIORITY_CLASS}}}}
        for name in peer_names:
            self.apps_v1.patch_namespaced_deployment(name=name, namespace=self.namespace, body=body)
        for name in peer_names:
            self._wait_for_deployment_ready(name, self.namespace)


class FileDescriptorExhaustionSaleor(FileDescriptorExhaustion):
    """``ulimit -n 1024`` on the Saleor API while a flood of idle TCP connections is held open.

    The original wrapped the Hotel Reservation frontend's entrypoint in
    ``ulimit -n 1024 && exec frontend`` and flooded it from the conductor with
    ~1,100 concurrent connections through a port-forward. Here the API's
    uvicorn command is wrapped the same way (each of its 4 worker processes
    inherits the 1024 soft limit), and the flood comes from a client pod in the
    hidden ``khaos`` namespace that keeps ~6,000 idle connections open to
    ``svc-saleor-api`` (uvicorn never times out a connection that has not sent
    a request). Workers hit EMFILE: accept() fails with ``Too many open files``
    and requests that need a new database/cache socket fail.
    """

    FLOODER = "api-connection-flood"
    CONNECTIONS = 6000
    FLOOD_SCRIPT = r"""
import os, socket, time
target = (os.environ["TARGET_HOST"], int(os.environ["TARGET_PORT"]))
want = int(os.environ["CONNECTIONS"])
held = []
while True:
    alive = []
    for s in held:
        try:
            s.setblocking(False)
            if s.recv(1) == b"":
                s.close()
                continue
        except BlockingIOError:
            pass
        except OSError:
            s.close()
            continue
        alive.append(s)
    held = alive
    opened = 0
    while len(held) < want and opened < 500:
        s = socket.socket()
        s.settimeout(1.0)
        try:
            s.connect(target)
            held.append(s)
        except OSError:
            s.close()
        opened += 1
    print(f"holding {len(held)} connections", flush=True)
    time.sleep(5)
"""

    def __init__(self, app_name: str = APP, faulty_service: str = API):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.forced_ulimit = 1024
        self.flooder_thread = None
        configure_port(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{faulty_service}` deployment (Saleor's GraphQL API behind `{API_SERVICE}`) is exhausting its "
                "file descriptors. Its container command was wrapped as `/bin/sh -c 'ulimit -n "
                f"{self.forced_ulimit} && exec uvicorn saleor.asgi:application ...'`, so each uvicorn worker may "
                f"hold at most {self.forced_ulimit} open files, while a client floods the service with thousands of "
                "simultaneous, idle TCP connections. The workers run out of descriptors: accept() fails with "
                "`OSError: [Errno 24] Too many open files` and requests that need new database or cache sockets "
                "fail, so storefront and checkout requests time out or error. The root cause is the insufficient "
                f"open-file limit of {self.forced_ulimit} (restore the plain uvicorn command / raise the limit)."
            ),
            oracle_factory=SaleorFDMitigationOracle,
        )

    def api_selector(self) -> str:
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        return ",".join(f"{k}={v}" for k, v in sorted(deployment.spec.selector.match_labels.items()))

    def _deployment_json(self) -> dict:
        return json.loads(
            self.kubectl.exec_command_checked(
                f"kubectl get deployment {self.faulty_service} -n {self.namespace} -o json"
            )
        )

    def _patch_container(self, command, args) -> None:
        patch = [
            {"op": "add", "path": "/spec/template/spec/containers/0/command", "value": command},
        ]
        patch.append(
            {"op": "add", "path": "/spec/template/spec/containers/0/args", "value": args}
            if args is not None
            else {"op": "remove", "path": "/spec/template/spec/containers/0/args"}
        )
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=json "
            f"-p {shlex.quote(json.dumps(patch))}"
        )
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=600s", timeout=630
        )

    def _flooder_manifest(self) -> dict:
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.FLOODER, "labels": {"app": self.FLOODER}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": self.FLOODER}},
                "template": {
                    "metadata": {"labels": {"app": self.FLOODER}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "flood",
                                "image": "python:3.12-alpine",
                                "command": ["python3", "-u", "-c", self.FLOOD_SCRIPT],
                                "env": [
                                    {
                                        "name": "TARGET_HOST",
                                        "value": f"{API_SERVICE}.{self.namespace}.svc.cluster.local",
                                    },
                                    {"name": "TARGET_PORT", "value": "8000"},
                                    {"name": "CONNECTIONS", "value": str(self.CONNECTIONS)},
                                ],
                            }
                        ],
                    },
                },
            },
        }

    @mark_fault_injected
    def inject_fault(self):
        container = self._deployment_json()["spec"]["template"]["spec"]["containers"][0]
        saved = {"command": container.get("command"), "args": container.get("args")}
        if not saved["command"]:
            raise RuntimeError(f"{self.faulty_service} has no explicit command to wrap")
        state_file(self, "fd-exhaustion").write_text(json.dumps(saved))
        entrypoint = " ".join(shlex.quote(part) for part in [*saved["command"], *(saved["args"] or [])])
        self._patch_container(["/bin/sh", "-c"], [f"ulimit -n {self.forced_ulimit} && exec {entrypoint}"])
        print(f"{self.faulty_service} now runs with ulimit -n {self.forced_ulimit}")
        ensure_namespace(self, HIDDEN_NAMESPACE)
        apply_manifest(self, self._flooder_manifest(), HIDDEN_NAMESPACE)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.FLOODER} -n {HIDDEN_NAMESPACE} --timeout=300s", timeout=330
        )
        print(f"{HIDDEN_NAMESPACE}/{self.FLOODER} holds up to {self.CONNECTIONS} connections to {API_SERVICE}")

    @mark_fault_injected
    def recover_fault(self):
        saved = json.loads(state_file(self, "fd-exhaustion").read_text())
        self.kubectl.exec_command(f"kubectl delete deployment {self.FLOODER} -n {HIDDEN_NAMESPACE} --ignore-not-found")
        self._patch_container(saved["command"], saved["args"])
        print(f"Restored the {self.faulty_service} command and stopped the connection flood")


class NamespaceMemoryLimitIA(Problem):
    """A namespace memory ResourceQuota blocks Saleor's recreated PostgreSQL pod.

    As in the original, the quota requires every container to declare memory.
    The database's own container does; its metrics-exporter sidecar does not, so
    admission rejects the recreated pod with ``must specify memory``.
    """

    QUOTA_NAME = NamespaceMemoryLimit.QUOTA_NAME
    MEMORY_QUOTA_KEYS = NamespaceMemoryLimit.MEMORY_QUOTA_KEYS
    MEMORY_LIMIT = NamespaceMemoryLimit.MEMORY_LIMIT
    rollout_timeout_seconds = 600

    def __init__(self, app_name: str = "saleor", statefulset: str = "postgres"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = statefulset
        configure_port(
            self,
            app_name,
            component=f"resourcequota/{self.QUOTA_NAME}",
            description=(
                f"Namespace-wide ResourceQuota `{self.QUOTA_NAME}` (`memory: {self.MEMORY_LIMIT}`) requires "
                "every container to declare memory, but not every workload does. When the "
                f"`{statefulset}` StatefulSet's pod was recreated, admission rejected it with `must specify "
                "memory` because its metrics-exporter sidecar has no memory request (the database container "
                "itself does), so the database is down and every request that needs it fails. Other "
                "noncompliant pods are vulnerable on their next restart; the quota's 1Gi is also below what the "
                "namespace already requests."
            ),
            oracle_factory=StatefulSetMemoryLimitMitigationOracle,
        )

    def _pod_name(self) -> str:
        return f"{self.faulty_service}-0"

    @mark_fault_injected
    def inject_fault(self):
        quotas = self.kubectl.get_resource_quotas(self.namespace)
        existing = [q.metadata.name for q in quotas if set(q.spec.hard or {}) & self.MEMORY_QUOTA_KEYS]
        if existing:
            raise RuntimeError(f"Cannot inject over existing memory ResourceQuota: {existing}")
        self.kubectl.apply_resource(
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": self.QUOTA_NAME, "namespace": self.namespace},
                "spec": {"hard": {"memory": self.MEMORY_LIMIT}},
            }
        )
        self.kubectl.exec_command_checked(
            f"kubectl delete pod {self._pod_name()} -n {self.namespace} --wait=true", timeout=180
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.faulty_service, self.namespace)
            if (sts.status.ready_replicas or 0) < (sts.spec.replicas or 1):
                print(f"StatefulSet {self.faulty_service} cannot recreate its pod under the quota")
                return
            time.sleep(2)
        raise RuntimeError(f"StatefulSet '{self.faulty_service}' remained ready after quota injection")

    @mark_fault_injected
    def recover_fault(self):
        quotas = self.kubectl.get_resource_quotas(self.namespace)
        if any(quota.metadata.name == self.QUOTA_NAME for quota in quotas):
            self.kubectl.delete_resource_quota(name=self.QUOTA_NAME, namespace=self.namespace)
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.faulty_service} -n {self.namespace} "
            f"--timeout={self.rollout_timeout_seconds}s",
            timeout=self.rollout_timeout_seconds + 30,
        )
