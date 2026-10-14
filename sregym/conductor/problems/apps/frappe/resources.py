"""Frappe: resources."""

from __future__ import annotations

import base64
import json
import shlex
import textwrap
import time

from kubernetes import client

from sregym.conductor.oracles.apps.frappe.state import FrappeFaultStateOracle
from sregym.conductor.oracles.apps.frappe.workers import DevShmFrappeOracle, FrappeWorkerHealthOracle
from sregym.conductor.problems.apps.frappe.helpers import APP, replace_pods_with_patch, rollout_status, selector_of
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import configure_port
from sregym.service.apps.frappe.application import Frappe
from sregym.service.runtime_images import REDIS_CLIENT_IMAGE
from sregym.utils.decorators import mark_fault_injected


class ValkeyMemoryDisruptionFrappe(Problem):
    """A Job floods Frappe's Redis cache with 1 MB values until it is OOM-killed.

    The cache (``frappe-redis-cache-master``, 192Mi limit) has no ``maxmemory``
    and keeps an append-only file on an emptyDir, so the container is OOM-killed
    and then OOM-killed again replaying its AOF while the flood continues.
    """

    JOB_NAME = "cache-memory-flood"
    CACHE = Frappe.REDIS_CACHE_STATEFULSET

    def __init__(self, app_name: str = APP):
        self.faulty_service = self.CACHE
        configure_port(
            self,
            app_name,
            component=f"statefulset/{self.CACHE}",
            description=(
                f"A Job (`{self.JOB_NAME}`) in the namespace floods Frappe's Redis cache (`{self.CACHE}`, Service "
                f"`{self.CACHE}`) with very large (1 MB) values from 10 threads. The cache has no `maxmemory` limit "
                "and runs with a 192Mi container memory limit, so its memory is exhausted and the container is "
                "OOMKilled and restarts (replaying the flooded append-only file), leaving the cache unavailable; "
                "gunicorn's sessions/cache lookups fail, so Desk/API requests error. Fix: stop the flood job and "
                "bring the cache back with its memory under control."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        script = textwrap.dedent(
            f"""
            import redis, threading, time
            def flood():
                c = redis.Redis(host='{self.CACHE}', port=6379)
                while True:
                    try:
                        c.set(f"key_{{time.time()}}", 'x' * 1000000)
                    except Exception as e:
                        print(f"Error: {{e}}"); time.sleep(1)
            ts = [threading.Thread(target=flood) for _ in range(10)]
            [t.start() for t in ts]; [t.join() for t in ts]
            """
        ).strip()
        encoded = base64.b64encode(script.encode()).decode()
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self.JOB_NAME, "namespace": self.namespace},
            "spec": {
                "backoffLimit": 1000,
                "template": {
                    "spec": {
                        "restartPolicy": "OnFailure",
                        "containers": [
                            {
                                "name": "flooder",
                                "image": REDIS_CLIENT_IMAGE,
                                "command": ["python3", "-c", f"import base64; exec(base64.b64decode('{encoded}'))"],
                            }
                        ],
                    }
                },
            },
        }
        baseline_restarts = self._cache_restarts()
        client.BatchV1Api().create_namespaced_job(self.namespace, job)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            restarts = self._cache_restarts()
            if restarts > baseline_restarts:
                print(f"{self.CACHE} restarted under the flood (restarts={restarts})")
                return
            time.sleep(5)
        print("⚠️ cache did not restart within 300s; proceeding")

    def _cache_restarts(self) -> int:
        pod = self.kubectl.core_v1_api.read_namespaced_pod(f"{self.CACHE}-0", self.namespace)
        return sum(cs.restart_count or 0 for cs in pod.status.container_statuses or [])

    @mark_fault_injected
    def recover_fault(self):
        self.kubectl.exec_command(
            f"kubectl delete job {self.JOB_NAME} -n {self.namespace} --cascade=foreground --ignore-not-found --wait=true"
        )
        # The flooded append-only file lives on the pod's emptyDir: a new pod starts empty.
        self.kubectl.exec_command(f"kubectl delete pod {self.CACHE}-0 -n {self.namespace} --wait=true")
        self.kubectl.exec_command_checked(
            f"kubectl rollout status statefulset/{self.CACHE} -n {self.namespace} --timeout=300s", timeout=330
        )

    def fault_check(self, oracle) -> dict | None:
        jobs = client.BatchV1Api().list_namespaced_job(self.namespace).items
        for job in jobs:
            if job.metadata.name == self.JOB_NAME and (job.status.active or 0) > 0:
                print(f"❌ Flood job {self.JOB_NAME} is still active")
                return oracle.fail("fault_still_present", job=self.JOB_NAME)
        sts = self.kubectl.apps_v1_api.read_namespaced_stateful_set(self.CACHE, self.namespace)
        if (sts.status.ready_replicas or 0) < (sts.spec.replicas or 1):
            print(f"❌ {self.CACHE} is not ready")
            return oracle.fail("fault_still_present", statefulset=self.CACHE, reason_detail="cache_not_ready")
        pong = self.app.redis("PING", cache=True).strip()
        if pong != "PONG":
            return oracle.fail("fault_still_present", statefulset=self.CACHE, ping=pong[:200])
        print(f"✅ No flood running and {self.CACHE} answers PING")
        return None


class DevShmExhaustionFrappe(Problem):
    """The RQ long worker stages ~80 MiB in /dev/shm before starting; the runtime default is 64 MiB."""

    shm_mount_path = "/dev/shm"
    scratch_mib = 80

    def __init__(self, app_name: str = APP, worker: str = Frappe.WORKER_DEPLOYMENT, container: str = "long"):
        self.worker_name = self.faulty_service = worker
        self.container = container
        configure_port(
            self,
            app_name,
            component=f"deployment/{worker}",
            description=(
                f"The `{worker}` deployment (Frappe's RQ worker for the long/default/short queues) stages about "
                f"{self.scratch_mib} MiB of scratch data in {self.shm_mount_path} when it starts, but its pod template "
                f"does not mount a memory-backed emptyDir (medium: Memory) at {self.shm_mount_path}. The container "
                "therefore gets the container runtime's default 64 MiB /dev/shm tmpfs; the write fails with ENOSPC "
                '("No space left on device"), the container exits non-zero and the deployment is in '
                "CrashLoopBackOff although the node has ample disk, so background jobs are no longer processed: the "
                "RQ queues back up until Frappe refuses new jobs (`Too many queued background jobs`, HTTP 503). "
                f"Fix: mount an emptyDir with medium: Memory at {self.shm_mount_path} (or roll back the worker "
                "template change that added the staging step)."
            ),
            oracle_factory=lambda problem: DevShmFrappeOracle(problem),
            health_oracle_factory=FrappeWorkerHealthOracle,
        )

    @property
    def worker_pod_selector(self) -> str:
        return selector_of(self, self.worker_name)

    def _container(self, deployment):
        return next(c for c in deployment.spec.template.spec.containers if c.name == self.container)

    @mark_fault_injected
    def inject_fault(self):
        dep = self.kubectl.get_deployment(self.worker_name, self.namespace)
        args = list(self._container(dep).args or [])
        stage = (
            f"set -e\n# Stage the worker's scratch spool in shared memory before starting.\n"
            f"dd if=/dev/zero of={self.shm_mount_path}/rq-spool bs=1M count={self.scratch_mib}\n"
            f"rm -f {self.shm_mount_path}/rq-spool\nset +e\n"
        )
        if args and "Stage the worker's scratch spool" in args[0]:
            new_args = args  # Already staged (a previous run); keep it once.
        else:
            new_args = [stage + args[0]] + args[1:] if args else [stage]
        container = {"name": self.container, "args": new_args}
        spec = {"containers": [container]}
        # A memory-backed /dev/shm left by an earlier repair would absorb the fault.
        mounts = [m for m in self._container(dep).volume_mounts or [] if m.mount_path == self.shm_mount_path]
        if mounts:
            container["volumeMounts"] = [{"mountPath": self.shm_mount_path, "$patch": "delete"}]
            spec["volumes"] = [{"name": m.name, "$patch": "delete"} for m in mounts]
        patch = {"spec": {"template": {"spec": spec}}}
        replace_pods_with_patch(self, self.worker_name, patch)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            pods = self.kubectl.core_v1_api.list_namespaced_pod(
                self.namespace, label_selector=self.worker_pod_selector
            ).items
            if any((cs.restart_count or 0) >= 1 for p in pods for cs in p.status.container_statuses or []):
                print(f"{self.worker_name} is crash-looping on /dev/shm ENOSPC")
                return
            time.sleep(5)
        print("⚠️ worker did not visibly crash within 180s")

    @mark_fault_injected
    def recover_fault(self):
        # The fix: a memory-backed /dev/shm (the staging step then fits).
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "128Mi"}}],
                        "containers": [
                            {"name": self.container, "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}]}
                        ],
                    }
                }
            }
        }
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.worker_name} -n {self.namespace} --type=strategic "
            f"-p {shlex.quote(json.dumps(patch))}"
        )
        rollout_status(self, self.worker_name)
