"""Page-cache thrashing noisy-neighbor problem for Hotel Reservation.

A synthetic batch scanner is co-located with the frontend pod and repeatedly
creates and reads a bounded working set. The workload stays Running, so
Kubernetes object health can look normal, but the latency-sensitive frontend
path shares node-level filesystem/page-cache pressure with the scanner.

Expected mitigation: remove, scale down, or move the batch scanner away from the
frontend node so the latency-sensitive path is no longer co-located with the
cache-thrashing workload.
"""

import contextlib
import textwrap
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.page_cache_thrash_mitigation_oracle import PageCacheThrashMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PageCacheThrashHotelReservation(Problem):
    thrasher_deployment = "cache-thrasher"
    thrasher_image = "python:3.12-alpine"
    thrasher_replicas = 1
    scratch_path = "/cache-thrash"
    scratch_mib = 768
    scan_chunk_kib = 1024

    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.frontend_node = None

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.thrasher_deployment}",
            namespace=self.namespace,
            description=(
                f"The cache-thrashing batch workload `{self.thrasher_deployment}` is co-located with the "
                "Hotel Reservation frontend pod. It repeatedly creates and scans a bounded file working set, "
                "creating node-level filesystem/page-cache pressure for the latency-sensitive frontend request path. "
                "Kubernetes pods and services can remain Running and Ready, but user-facing requests become slower "
                "or unreliable because the frontend shares the node with the cache-scanning workload. Mitigation is "
                "to remove, scale down, or move the batch workload away from the frontend node."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = PageCacheThrashMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._delete_thrasher()
        self.frontend_node = self.find_frontend_node()
        print(f"Frontend node: {self.frontend_node}")
        self.apps_v1.create_namespaced_deployment(self.namespace, self._thrasher_deployment())
        self._wait_for_deployment(self.thrasher_deployment, self.thrasher_replicas)
        print(
            f"Created cache-thrashing deployment '{self.thrasher_deployment}' "
            f"on frontend node '{self.frontend_node}'."
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._delete_thrasher()
        print(f"Removed cache-thrashing deployment '{self.thrasher_deployment}'.")

    def find_frontend_node(self) -> str:
        pods = self.core_v1.list_namespaced_pod(
            self.namespace,
            label_selector="io.kompose.service=frontend",
        ).items
        for pod in pods:
            if pod.status.phase == "Running" and pod.spec.node_name:
                return pod.spec.node_name
        raise RuntimeError("Could not find a running frontend pod with an assigned node.")

    def _thrasher_deployment(self) -> dict:
        spec = {
            "nodeName": self.frontend_node,
            "terminationGracePeriodSeconds": 0,
            "automountServiceAccountToken": False,
            "containers": [self._thrasher_container()],
            "volumes": [
                {
                    "name": "cache-thrash-data",
                    "emptyDir": {},
                }
            ],
        }
        return {
            "metadata": {
                "name": self.thrasher_deployment,
                "labels": {"app": self.thrasher_deployment},
            },
            "spec": {
                "replicas": self.thrasher_replicas,
                "selector": {"matchLabels": {"app": self.thrasher_deployment}},
                "template": {
                    "metadata": {"labels": {"app": self.thrasher_deployment}},
                    "spec": spec,
                },
            },
        }

    def _thrasher_container(self) -> dict:
        script = textwrap.dedent("""\
            import os
            import time

            path = os.environ["SCRATCH_PATH"]
            scratch_mib = int(os.environ["SCRATCH_MIB"])
            chunk_kib = int(os.environ["SCAN_CHUNK_KIB"])
            chunk = b"x" * (chunk_kib * 1024)
            os.makedirs(path, exist_ok=True)

            target = os.path.join(path, "working-set.bin")
            if not os.path.exists(target) or os.path.getsize(target) < scratch_mib * 1024 * 1024:
                with open(target, "wb", buffering=0) as f:
                    for _ in range((scratch_mib * 1024) // chunk_kib):
                        f.write(chunk)

            while True:
                with open(target, "rb", buffering=0) as f:
                    while f.read(len(chunk)):
                        pass
                time.sleep(0.05)
            """)
        return {
            "name": "cache-thrasher",
            "image": self.thrasher_image,
            "command": ["python", "-c", script],
            "env": [
                {"name": "SCRATCH_PATH", "value": self.scratch_path},
                {"name": "SCRATCH_MIB", "value": str(self.scratch_mib)},
                {"name": "SCAN_CHUNK_KIB", "value": str(self.scan_chunk_kib)},
            ],
            "volumeMounts": [
                {
                    "name": "cache-thrash-data",
                    "mountPath": self.scratch_path,
                }
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

    def _delete_thrasher(self):
        with contextlib.suppress(ApiException):
            self.apps_v1.delete_namespaced_deployment(
                self.thrasher_deployment,
                self.namespace,
                grace_period_seconds=0,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                self.apps_v1.read_namespaced_deployment(self.thrasher_deployment, self.namespace)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(2)
