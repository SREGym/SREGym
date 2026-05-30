"""Shared-memory (/dev/shm) exhaustion on Hotel Reservation.

A media-processing worker writes a scratch buffer larger than the container
runtime's default 64 MiB ``/dev/shm`` tmpfs. Because the pod template does not
request a memory-backed ``emptyDir`` at ``/dev/shm``, the write fails with
``ENOSPC`` ("No space left on device"), the container exits non-zero, and the
deployment falls into ``CrashLoopBackOff`` -- even though the node's filesystem
has plenty of free disk space.

This reproduces a classic real-world abstraction leak: applications that rely on
POSIX shared memory (PostgreSQL parallel queries, ML data loaders, Chromium,
OpenCV/FFmpeg) crash with a *disk* error message while the disk is nearly empty.
The intended fix is to mount an ``emptyDir`` with ``medium: Memory`` at
``/dev/shm``; restarting or deleting the worker does not fix the underlying
misconfiguration.
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
    """Inject a /dev/shm exhaustion fault that crash-loops a worker deployment.

    The worker (generic name ``media-processor`` so the benchmark/fault is not
    revealed to the agent) writes ``scratch_mib`` MiB into ``/dev/shm``. With the
    default 64 MiB shm tmpfs this overflows and the container crash-loops.
    """

    worker_name = "media-processor"
    worker_image = "busybox:1.36"
    shm_mount_path = "/dev/shm"
    # How much the worker writes to /dev/shm. Larger than the 64 MiB default so it
    # overflows; the intended fix provisions a memory-backed shm comfortably above
    # this (e.g. 256Mi).
    scratch_mib = 128

    def __init__(self):
        self.app = HotelReservation()
        # app must be set before super().__init__() so the base can read app.namespace
        super().__init__(app=self.app, namespace=self.app.namespace)

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
        # Start from a clean slate in case of a re-run.
        self._delete_worker()
        self.apps_v1.create_namespaced_deployment(self.namespace, self._worker_deployment())
        print(f"Created worker '{self.worker_name}' with default 64 MiB /dev/shm | Namespace: {self.namespace}")
        # Best-effort: wait until the symptom (crash-loop) is actually visible so the
        # agent does not start investigating a still-pulling pod.
        self._wait_for_worker_unhealthy(timeout=120)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._delete_worker()
        print(f"Removed worker '{self.worker_name}' | Namespace: {self.namespace}")

    # ----------------------------------------------------------------- helpers

    def _worker_deployment(self) -> dict:
        # The command writes scratch_mib MiB into /dev/shm then idles. On a 64 MiB
        # shm this fails with ENOSPC and the container exits non-zero -> CrashLoop.
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
        # Wait for the deployment to actually disappear so a subsequent create
        # does not race with a pending deletion.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                self.apps_v1.read_namespaced_deployment(self.worker_name, self.namespace)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(2)
