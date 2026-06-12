"""Shared-memory (/dev/shm) exhaustion on Hotel Reservation.

A media-processing worker writes more than the container runtime's default 64 MiB
/dev/shm tmpfs allows. The write fails with ENOSPC, the container exits non-zero,
and the deployment enters CrashLoopBackOff -- even though the node disk is nearly empty.
Fix: mount an emptyDir with medium: Memory at /dev/shm.
"""

import contextlib
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.dev_shm_mitigation_oracle import DevShmMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class DevShmExhaustionHotelReservation(Problem):
    """Inject a /dev/shm exhaustion fault that crash-loops a worker deployment."""

    worker_name = "media-processor"
    worker_image = "busybox:1.36"
    shm_mount_path = "/dev/shm"
    scratch_mib = 128

    def __init__(self):
        super().__init__(app=HotelReservation())

        self.kubectl = KubeCtl()
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.worker_name}",
            namespace=self.namespace,
            description=(
                f"The {self.worker_name} deployment writes about {self.scratch_mib} MiB of scratch data to "
                f"{self.shm_mount_path}, but its pod template does not mount a memory-backed emptyDir "
                f"(medium: Memory) at {self.shm_mount_path}. The container therefore falls back to the "
                "container runtime's default 64 MiB /dev/shm tmpfs. Writes beyond 64 MiB fail with ENOSPC "
                '("No space left on device"), so the container exits non-zero and the deployment enters '
                "CrashLoopBackOff, even though the node's filesystem has ample free disk space."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = DevShmMitigationOracle(problem=self)

        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._delete_worker()
        self.apps_v1.create_namespaced_deployment(self.namespace, self._worker_deployment())
        print(f"Created worker '{self.worker_name}' with default 64 MiB /dev/shm | Namespace: {self.namespace}")
        self._wait_for_worker_unhealthy(timeout=120)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._delete_worker()
        print(f"Removed worker '{self.worker_name}' | Namespace: {self.namespace}")

    def _worker_deployment(self) -> dict:
        command = (
            f"dd if=/dev/zero of={self.shm_mount_path}/scratch bs=1M count={self.scratch_mib} && tail -f /dev/null"
        )
        container = {
            "name": "worker",
            "image": self.worker_image,
            "command": ["sh", "-c", command],
        }
        pod_spec = {
            "terminationGracePeriodSeconds": 0,
            "automountServiceAccountToken": False,
            "containers": [container],
        }
        return {
            "metadata": {"name": self.worker_name, "labels": {"app": self.worker_name}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": self.worker_name}},
                "template": {"metadata": {"labels": {"app": self.worker_name}}, "spec": pod_spec},
            },
        }

    def _wait_for_worker_unhealthy(self, timeout: int = 120):
        """Poll until the worker has crashed at least once (best-effort)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pods = self.core_v1.list_namespaced_pod(self.namespace, label_selector=f"app={self.worker_name}").items
            for pod in pods:
                for cs in pod.status.container_statuses or []:
                    waiting = cs.state.waiting
                    terminated = cs.state.terminated
                    if (cs.restart_count or 0) >= 1:
                        print(f"Worker is crash-looping (restarts={cs.restart_count}).")
                        return
                    if waiting and waiting.reason in ("CrashLoopBackOff", "Error"):
                        print(f"Worker is unhealthy (reason={waiting.reason}).")
                        return
                    if terminated and terminated.reason != "Completed":
                        print(f"Worker container terminated (reason={terminated.reason}).")
                        return
            time.sleep(3)
        print("⚠️ Worker did not visibly crash within timeout; proceeding anyway.")

    def _delete_worker(self):
        with contextlib.suppress(ApiException):
            self.apps_v1.delete_namespaced_deployment(self.worker_name, self.namespace, grace_period_seconds=0)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                self.apps_v1.read_namespaced_deployment(self.worker_name, self.namespace)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(2)
