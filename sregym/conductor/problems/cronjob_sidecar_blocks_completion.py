"""Problem: a CronJob whose pod has a regular (non-native) sidecar container
prevents Job completion. Accumulated active Jobs are never reaped.

Real-world failure class
------------------------
A Kubernetes Job is only marked ``Complete`` when *every* container in the pod
terminates. When a CronJob's pod includes a sidecar designed for long-running
service work (a service-mesh proxy such as ``istio-proxy`` / ``linkerd-proxy``,
or a telemetry sidecar such as ``fluent-bit`` / ``fluentd`` / ``otel-collector``
/ ``datadog-agent``), the sidecar never receives a termination signal when the
primary container exits. The pod stays ``Running`` indefinitely and the Job
stays ``active``. Because ``successfulJobsHistoryLimit`` only applies to
*finished* Jobs, accumulated active Jobs are not reaped. Over hours the
namespace fills with hundreds of stuck Jobs and Pods.

This problem is officially documented by the Kubernetes project and was
significant enough to motivate the introduction of native sidecar containers in
Kubernetes 1.28 (KEP-753, ``initContainers`` with ``restartPolicy: Always``).
On clusters where workloads have not opted in to the native pattern — which is
still the majority of production manifests, Helm charts, and operator-managed
deployments — the legacy pattern continues to reproduce the same bug exactly as
reported in kubernetes/kubernetes#64056.

References
~~~~~~~~~~
* Kubernetes blog (2023-08-25), "Introducing native sidecar containers":
  https://kubernetes.io/blog/2023/08/25/native-sidecar-containers/
* KEP-753 (Sidecar containers):
  https://github.com/kubernetes/enhancements/blob/master/keps/sig-node/753-sidecar-containers/README.md
* kubernetes/kubernetes#64056 ("CronJob successfulJobsHistoryLimit and
  failedJobsHistoryLimit not working") — confirmed root cause: a non-
  terminating fluentd sidecar.
* istio/istio#11045 ("Jobs are Broken (they Never Complete)").
* Linkerd blog (2026-05-18), "The Proxy Died First: How Kubernetes Native
  Sidecars Solve the Service Mesh Shutdown Problem".
* TeamSnap Engineering, "Properly Running Kubernetes Jobs with Sidecars".

Simulation in SREGym
--------------------
A CronJob named ``audit-log-archiver`` is deployed into the Hotel Reservation
namespace. Its pod template contains:

* A primary container (``archiver``) that simulates a short, well-behaved audit
  log archival step — it logs, sleeps 1 second, and exits 0.
* A sidecar container (``fluent-bit-sidecar``) that simulates a fluent-bit log
  shipper running as a regular (non-native) sidecar — it logs and sleeps in an
  infinite loop, exactly as a real fluent-bit process would when configured to
  forward logs to a remote endpoint.

The CronJob's ``schedule`` is ``* * * * *`` (every minute). Each scheduled Job
runs the primary, which exits, but the sidecar keeps the pod alive forever.
After three to four schedule cycles the symptom is unambiguous: several Jobs
stuck at ``COMPLETIONS: 0/1`` whose pods show ``archiver`` ``Terminated/Completed``
and ``fluent-bit-sidecar`` ``Running``.

Accepted mitigation (enforced by the oracle)
--------------------------------------------
Convert the sidecar to the Kubernetes 1.28+ native sidecar pattern: move
it from ``spec.containers`` into ``spec.initContainers`` and set
``restartPolicy: Always`` on it. This is the architecturally correct fix
per KEP-753 -- K8s itself auto-terminates the sidecar after the primary
container exits, so the Pod reaches ``Succeeded`` and the Job reaches
``Complete``.

Rejected mitigations
~~~~~~~~~~~~~~~~~~~~
* Adding ``activeDeadlineSeconds`` to the jobTemplate. Time-bombs the
  workload rather than fixing the lifecycle bug. Real archival runs that
  exceed the deadline are killed mid-flight.
* Removing the sidecar container. Silently breaks audit-log forwarding
  to the SIEM.
* Deleting the CronJob. Removes the workload, not the bug.

Whichever fix the agent applies, it must also clean up the already-
accumulated active Jobs -- otherwise the oracle's "active Jobs ≤ N"
check still fails because the historical Jobs are still active.
"""

import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.cronjob_sidecar_mitigation import (
    CronJobSidecarBlocksCompletionMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class CronJobSidecarBlocksCompletionHotelReservation(Problem):
    """A CronJob whose pod has a non-terminating sidecar causes active Jobs to
    accumulate in the Hotel Reservation namespace."""

    CRONJOB_NAME = "audit-log-archiver"
    PRIMARY_CONTAINER = "archiver"
    SIDECAR_CONTAINER = "fluent-bit-sidecar"
    SCHEDULE = "* * * * *"

    # Wait until at least this many Jobs from the CronJob exist before
    # considering the symptom established. Each schedule fires once per minute,
    # so this is roughly a 2.5-minute polling window.
    MIN_JOBS_FOR_VISIBLE_SYMPTOM = 2
    JOB_ACCUMULATION_TIMEOUT_S = 240
    JOB_POLL_INTERVAL_S = 10

    # Recovery polling.
    RECOVERY_TIMEOUT_S = 180
    RECOVERY_POLL_INTERVAL_S = 5

    def __init__(self, faulty_service: str = "audit-log-archiver"):
        # The "faulty service" here is the CronJob itself, not one of the app's
        # microservices. The app's microservices remain healthy throughout — the
        # fault is namespace-level resource accumulation, not service breakage.
        self.faulty_service = faulty_service
        self.cronjob_name = self.CRONJOB_NAME

        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.namespace = self.app.namespace
        self.kubectl = KubeCtl()
        self.batch_v1 = client.BatchV1Api()
        self.core_v1 = client.CoreV1Api()

        self.root_cause = self.build_structured_root_cause(
            component=f"CronJob/{self.CRONJOB_NAME}",
            namespace=self.namespace,
            description=(
                f"The CronJob '{self.CRONJOB_NAME}' in namespace '{self.namespace}' "
                "schedules a pod every minute. Its jobTemplate contains two regular "
                f"containers: a primary container ('{self.PRIMARY_CONTAINER}') that "
                f"performs a short archival step and exits cleanly, and a sidecar "
                f"container ('{self.SIDECAR_CONTAINER}') that runs a long-running "
                "log-shipper process. Because Kubernetes considers a Job 'Complete' "
                "only when every container in the pod terminates, and the sidecar "
                "is a regular (non-native) container with no termination handling, "
                "the Pod stays Running indefinitely after the primary exits. Each "
                "schedule produces a new active Job that never completes. "
                "'successfulJobsHistoryLimit' does not apply to active Jobs, so they "
                "accumulate without bound, visible as Jobs stuck at COMPLETIONS=0/1 "
                "and pods whose primary container shows Terminated/Completed while "
                "the sidecar shows Running. The acceptable fix is to convert the "
                "sidecar to the Kubernetes 1.28+ native pattern by moving it to "
                "initContainers with restartPolicy=Always (KEP-753); the kubelet "
                "then SIGTERMs the sidecar after the primary exits, allowing the "
                "Pod to reach Succeeded and the Job to reach Complete. "
                "activeDeadlineSeconds (time-bombs the workload), removing the "
                "sidecar container (silently breaks log forwarding to the SIEM), "
                "and deleting the CronJob entirely (removes the workload) are all "
                "rejected. The agent must also clean up the already-accumulated "
                "active Jobs."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # The Hotel Reservation app is deployed up-front so the oracle can
        # verify the application is still healthy after the agent's mitigation.
        self.app.create_workload()

        self.mitigation_oracle = CronJobSidecarBlocksCompletionMitigationOracle(problem=self)

    # ------------------------------------------------------------------
    # Fault injection
    # ------------------------------------------------------------------
    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        body = self._build_cronjob_body()

        # Replace any leftover CronJob with the same name (defensive — handles
        # re-running inject after a failed previous run).
        try:
            self.batch_v1.delete_namespaced_cron_job(
                name=self.CRONJOB_NAME,
                namespace=self.namespace,
                propagation_policy="Foreground",
            )
            self._wait_for_cronjob_absent()
        except ApiException as e:
            if e.status != 404:
                raise

        self.batch_v1.create_namespaced_cron_job(namespace=self.namespace, body=body)
        print(
            f"Created CronJob '{self.CRONJOB_NAME}' in namespace '{self.namespace}' "
            f"(schedule={self.SCHEDULE}, primary='{self.PRIMARY_CONTAINER}', "
            f"sidecar='{self.SIDECAR_CONTAINER}')."
        )

        # Wait until at least MIN_JOBS_FOR_VISIBLE_SYMPTOM Jobs from the
        # CronJob exist. This proves the fault is producing the expected
        # accumulation symptom before we hand control to the agent.
        deadline = time.monotonic() + self.JOB_ACCUMULATION_TIMEOUT_S
        last_count = -1
        while time.monotonic() < deadline:
            count = self._count_owned_jobs()
            if count != last_count:
                print(f"[inject] CronJob-owned Jobs: {count}")
                last_count = count
            if count >= self.MIN_JOBS_FOR_VISIBLE_SYMPTOM:
                print(
                    f"Fault visible: {count} active Jobs from '{self.CRONJOB_NAME}', "
                    "each with sidecar holding the pod open."
                )
                break
            time.sleep(self.JOB_POLL_INTERVAL_S)
        else:
            raise RuntimeError(
                f"Timed out waiting for {self.MIN_JOBS_FOR_VISIBLE_SYMPTOM} Jobs to "
                f"accumulate within {self.JOB_ACCUMULATION_TIMEOUT_S}s. The CronJob "
                "controller may not be scheduling."
            )

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    # ------------------------------------------------------------------
    # Fault recovery
    # ------------------------------------------------------------------
    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # Capture the names of Jobs that pre-date the recovery so we can
        # delete only those, not new Jobs the (now-fixed) CronJob will
        # legitimately schedule going forward.
        accumulated = [j.metadata.name for j in self._list_owned_jobs()]

        # Apply the only accepted fix: convert the sidecar to the K8s 1.28+
        # native pattern (move it to initContainers with
        # restartPolicy=Always). The cleanest way to apply this is to
        # delete and recreate the CronJob with the native-sidecar body,
        # which also covers the case where the agent's rejected mitigation
        # already deleted the CronJob.
        body = self._build_cronjob_body_with_native_sidecar()
        try:
            self.batch_v1.delete_namespaced_cron_job(
                name=self.CRONJOB_NAME,
                namespace=self.namespace,
                propagation_policy="Foreground",
            )
            self._wait_for_cronjob_absent()
        except ApiException as e:
            if e.status != 404:
                raise

        self.batch_v1.create_namespaced_cron_job(namespace=self.namespace, body=body)
        print(
            f"Recreated CronJob '{self.CRONJOB_NAME}' with the native sidecar pattern "
            "(initContainers + restartPolicy=Always)."
        )

        # Clean up Jobs that accumulated before the recovery patch went in.
        # New Jobs the (now-fixed) CronJob spawns are fine and will
        # actually complete because the sidecar terminates on time.
        for name in accumulated:
            try:
                self.batch_v1.delete_namespaced_job(
                    name=name,
                    namespace=self.namespace,
                    propagation_policy="Foreground",
                )
            except ApiException as e:
                if e.status != 404:
                    print(f"  warning: could not delete Job {name}: {e!r}")

        # Wait for the specifically named pre-recovery Jobs to be gone.
        self._wait_for_specific_jobs_absent(accumulated)

        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_cronjob_body(self) -> dict:
        # Realistic primary container: simulates an audit-log archive step.
        primary_cmd = (
            "echo \"[$(date -u '+%FT%TZ')] archiver: starting audit log archive\"\n"
            "echo \"[$(date -u '+%FT%TZ')] archiver: bundling /var/log/audit\"\n"
            "sleep 1\n"
            "echo \"[$(date -u '+%FT%TZ')] archiver: upload complete (0 bytes archived)\"\n"
        )
        # Realistic sidecar: a network listener bound to the fluent-bit forward
        # port (24224). In production this would be the actual fluent-bit binary
        # accepting log streams from other pods; we use busybox `nc` here for
        # portability inside kind. The pattern that reproduces the bug is that
        # this daemon never exits on its own when the primary container finishes
        # -- it just keeps waiting on the listening socket, exactly as a real
        # log-forwarder daemon does. (Crucially, the sidecar does not run a
        # visible `while`/`sleep` loop; an agent reading the manifest sees a
        # network service, not a shell loop.)
        sidecar_cmd = (
            "echo \"[$(date -u '+%FT%TZ')] fluent-bit: binding to forward port 24224\"\n"
            "echo \"[$(date -u '+%FT%TZ')] fluent-bit: ready to receive log streams\"\n"
            "exec nc -lk -p 24224 -e cat\n"
        )

        return {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": self.CRONJOB_NAME,
                "namespace": self.namespace,
                "labels": {
                    "app.kubernetes.io/name": self.CRONJOB_NAME,
                    "app.kubernetes.io/component": "audit",
                    "app.kubernetes.io/part-of": "compliance",
                },
            },
            "spec": {
                "schedule": self.SCHEDULE,
                "concurrencyPolicy": "Allow",
                "successfulJobsHistoryLimit": 3,
                "failedJobsHistoryLimit": 1,
                "jobTemplate": {
                    "spec": {
                        "backoffLimit": 0,
                        "template": {
                            "metadata": {
                                "labels": {
                                    "app.kubernetes.io/name": self.CRONJOB_NAME,
                                },
                            },
                            "spec": {
                                "restartPolicy": "Never",
                                "containers": [
                                    {
                                        "name": self.PRIMARY_CONTAINER,
                                        "image": "busybox:1.36",
                                        "command": ["sh", "-c", primary_cmd],
                                        "resources": {
                                            "requests": {"cpu": "10m", "memory": "16Mi"},
                                            "limits": {"cpu": "50m", "memory": "32Mi"},
                                        },
                                    },
                                    {
                                        "name": self.SIDECAR_CONTAINER,
                                        "image": "busybox:1.36",
                                        "command": ["sh", "-c", sidecar_cmd],
                                        "resources": {
                                            "requests": {"cpu": "10m", "memory": "16Mi"},
                                            "limits": {"cpu": "50m", "memory": "32Mi"},
                                        },
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        }

    def _build_cronjob_body_with_native_sidecar(self) -> dict:
        """Same workload, but with the sidecar moved to ``initContainers`` and
        given ``restartPolicy: Always``. This is the Path 3 (KEP-753) shape
        that the oracle accepts. Used by ``recover_fault``."""
        body = self._build_cronjob_body()
        pod_spec = body["spec"]["jobTemplate"]["spec"]["template"]["spec"]

        containers = pod_spec["containers"]
        sidecar_idx = next(i for i, c in enumerate(containers) if c["name"] == self.SIDECAR_CONTAINER)
        sidecar = containers.pop(sidecar_idx)
        sidecar["restartPolicy"] = "Always"
        pod_spec["initContainers"] = [sidecar]
        return body

    def _list_owned_jobs(self) -> list:
        jobs = self.batch_v1.list_namespaced_job(namespace=self.namespace)
        return [
            j
            for j in jobs.items
            if any(
                ref.kind == "CronJob" and ref.name == self.CRONJOB_NAME for ref in (j.metadata.owner_references or [])
            )
        ]

    def _count_owned_jobs(self) -> int:
        return len(self._list_owned_jobs())

    def _wait_for_cronjob_absent(self):
        deadline = time.monotonic() + self.RECOVERY_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                self.batch_v1.read_namespaced_cron_job(name=self.CRONJOB_NAME, namespace=self.namespace)
            except ApiException as e:
                if e.status == 404:
                    return
                raise
            time.sleep(self.RECOVERY_POLL_INTERVAL_S)
        print(
            f"⚠️ Timed out waiting for CronJob '{self.CRONJOB_NAME}' to be deleted; "
            "leaving for next inject's defensive delete."
        )

    def _wait_for_owned_jobs_absent(self):
        deadline = time.monotonic() + self.RECOVERY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._count_owned_jobs() == 0:
                return
            time.sleep(self.RECOVERY_POLL_INTERVAL_S)
        leftover = self._count_owned_jobs()
        if leftover:
            print(f"⚠️ {leftover} Job(s) owned by '{self.CRONJOB_NAME}' still present after recovery timeout.")

    def _wait_for_specific_jobs_absent(self, job_names):
        """Wait until none of the listed Job names exist any more. New Jobs
        the CronJob may have legitimately scheduled in the meantime are
        ignored."""
        if not job_names:
            return
        target = set(job_names)
        deadline = time.monotonic() + self.RECOVERY_TIMEOUT_S
        while time.monotonic() < deadline:
            existing = {j.metadata.name for j in self._list_owned_jobs()}
            if not (existing & target):
                return
            time.sleep(self.RECOVERY_POLL_INTERVAL_S)
        remaining = {j.metadata.name for j in self._list_owned_jobs()} & target
        if remaining:
            print(f"⚠️ {len(remaining)} pre-recovery Job(s) still present after recovery timeout: {sorted(remaining)}")
