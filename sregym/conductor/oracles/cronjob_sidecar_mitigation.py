"""Mitigation oracle for the ``cronjob_sidecar_blocks_completion`` problem.

This oracle is purpose-built because the default ``MitigationOracle`` (which
walks every pod and requires phase == "Running") cannot model the post-fix
state of a Job/CronJob fault:

* After a correct mitigation, pods from Jobs that ran successfully will be in
  the ``Succeeded`` phase (not ``Running``). A naive pod walk would mark these
  as failures even though the application is healthy.
* The fault's failure mode is unbounded growth in *active* Jobs, not crashed
  pods. The oracle must reason about the Job/CronJob spec, not just pod phase.

The oracle accepts exactly one fix: the Kubernetes 1.28+ native sidecar
pattern (KEP-753), in which the sidecar is moved to ``initContainers`` and
given ``restartPolicy: Always``. K8s itself auto-terminates the sidecar after
the primary container exits, so the Job can reach ``Complete``. Workarounds
that resolve the symptom by eliminating the workload's functional purpose
(removing the sidecar, deleting the CronJob) or by putting a time bomb on the
Job (``activeDeadlineSeconds``) are all rejected.

The oracle checks four independent properties:

1. **Spec is fixed.** The CronJob's jobTemplate now uses the native sidecar
   pattern.
2. **Accumulated Jobs are gone.** Active Jobs created from the old regular-
   sidecar template are rejected. A small number of current-template Jobs may
   be active at a schedule boundary.
3. **App still healthy.** Every Deployment in the namespace reports
   ``ready_replicas == spec.replicas``. We check Deployment status directly
   rather than walking pods so ``Succeeded`` Job pods don't produce false
   negatives.
4. **Current template works at runtime.** The oracle creates one fresh Job from
   the current CronJob template and requires that exact Job to succeed. Old
   completed Jobs cannot satisfy this proof.
"""

import contextlib
import copy
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.base import Oracle

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5
_MAX_ACTIVE_JOBS = 2  # allow legitimate schedule-boundary overlap after rejecting stale templates

# How long to wait for the controlled Job created from the current template.
# A correctly configured native sidecar should let the Job reach Succeeded
# within one terminationGracePeriodSeconds window (default 30s).
_BEHAVIOR_PROOF_TIMEOUT_S = 90
_BEHAVIOR_PROOF_POLL_INTERVAL_S = 5


class CronJobSidecarBlocksCompletionMitigationOracle(Oracle):
    """Oracle for the CronJob-sidecar-blocks-completion fault.

    Attributes inherited from the Problem (set in the Problem's ``__init__``):
        problem.namespace: the application namespace.
        problem.cronjob_name: the name of the runaway CronJob.
    """

    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.batch_v1 = client.BatchV1Api()
        self.apps_v1 = client.AppsV1Api()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def evaluate(self, *args, **kwargs) -> dict:
        print("== CronJob Sidecar Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        cronjob_name = self.problem.cronjob_name

        # Let any in-progress app rollouts settle so we don't evaluate a
        # transient window where the agent's patch is mid-application.
        self._wait_for_rollouts(kubectl, namespace)

        # 1. Was the CronJob fixed using the native sidecar pattern?
        cj = self._get_cronjob(cronjob_name, namespace)
        if not self._is_spec_fixed(cj):
            if cj is None:
                return self._fail(
                    f"CronJob '{cronjob_name}' was deleted. Deletion is not an acceptable "
                    "fix here -- the audit-log archival workload exists for a real reason "
                    "(compliance log forwarding) and the bug is the Pod lifecycle, not the "
                    "workload. Convert the sidecar to the K8s 1.28+ native pattern: move "
                    "it to spec.jobTemplate.spec.template.spec.initContainers with "
                    "restartPolicy=Always."
                )
            return self._fail(
                f"CronJob '{cronjob_name}' is not in an acceptable post-fix state. The "
                "only accepted fix for this lifecycle bug is the K8s 1.28+ native sidecar "
                "pattern: move the sidecar container from spec.jobTemplate.spec.template."
                "spec.containers to spec.jobTemplate.spec.template.spec.initContainers, "
                "and set restartPolicy=Always on it. activeDeadlineSeconds and removing "
                "the sidecar are not accepted -- they either time-bomb the workload or "
                "delete its functional purpose."
            )

        # 2. Were Jobs created from the old, blocking template cleaned up?
        active_jobs = self._active_jobs(cronjob_name, namespace)
        stale_jobs = [job.metadata.name for job in active_jobs if not self._job_spec_is_safe(job.spec)]
        if stale_jobs:
            return self._fail(
                f"Active Jobs still use the old blocking sidecar template and must be cleaned up: {sorted(stale_jobs)}"
            )

        n_active = len(active_jobs)
        if n_active > _MAX_ACTIVE_JOBS:
            return self._fail(
                f"{n_active} Jobs owned by '{cronjob_name}' are still active "
                f"(threshold: {_MAX_ACTIVE_JOBS}). Accumulated Jobs must be cleaned up."
            )

        # 3. Is the rest of the namespace's application still healthy?
        problem_dep = self._unhealthy_deployment(namespace)
        if problem_dep is not None:
            return self._fail(
                f"Deployment '{problem_dep}' in '{namespace}' is under-replicated; "
                "agent's mitigation produced collateral damage to the application."
            )

        # 4. Prove the current template, not historical Job state. Create one
        # controlled Job directly from the current CronJob's Job template.
        print(f"Waiting up to {_BEHAVIOR_PROOF_TIMEOUT_S}s for the current template to complete a Job...")
        if not self._run_controlled_job(cj, namespace):
            return self._fail(
                f"The current template for '{cronjob_name}' did not complete a fresh Job within "
                f"{_BEHAVIOR_PROOF_TIMEOUT_S}s. Check that the real sidecar is functional and uses "
                "the native sidecar lifecycle."
            )

        print(
            f"✅ Spec fixed ({self._fix_description(cj)}); "
            f"{n_active} active Jobs ≤ {_MAX_ACTIVE_JOBS}; app healthy; "
            "a fresh Job reached Complete (current template verified at runtime)"
        )
        return {"success": True}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _wait_for_rollouts(self, kubectl, namespace):
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = kubectl.list_deployments(namespace)
            all_settled = True
            for dep in deployments.items:
                status = dep.status
                desired = dep.spec.replicas or 1
                if (
                    (status.updated_replicas or 0) < desired
                    or (status.ready_replicas or 0) < desired
                    or (status.unavailable_replicas or 0) > 0
                ):
                    all_settled = False
                    break
            if all_settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)
        print("⚠️ Timed out waiting for deployments to settle; evaluating current state")

    def _get_cronjob(self, name, namespace):
        try:
            return self.batch_v1.read_namespaced_cron_job(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _is_spec_fixed(self, cronjob) -> bool:
        """Return True only if the CronJob has been converted to the K8s 1.28+
        native sidecar pattern.

        The accepted post-fix shape keeps the named primary container and moves
        the named fluent-bit container from regular ``containers`` to
        ``initContainers`` with ``restartPolicy: Always``. The CronJob must
        remain enabled on its original schedule, without a Job or Pod active
        deadline.

        Explicitly rejected shapes:

        * CronJob deleted entirely: removes the workload, not the bug.
        * Sidecar removed from ``containers``: deletes the workload's
          functional purpose (audit-log forwarding to the SIEM).
        * ``activeDeadlineSeconds`` added to the jobTemplate: time-bombs the
          workload rather than fixing the lifecycle bug. Real archival runs
          that take longer than the deadline get killed.
        """
        if cronjob is None:
            return False
        if bool(cronjob.spec.suspend):
            return False
        if cronjob.spec.schedule != self.problem.SCHEDULE:
            return False
        return self._job_spec_is_safe(cronjob.spec.job_template.spec)

    def _job_spec_is_safe(self, job_spec) -> bool:
        if (job_spec.active_deadline_seconds or 0) > 0:
            return False

        pod_spec = job_spec.template.spec
        if (pod_spec.active_deadline_seconds or 0) > 0:
            return False

        regular_names = [container.name for container in pod_spec.containers or []]
        native_sidecars = [
            container
            for container in pod_spec.init_containers or []
            if container.name == self.problem.SIDECAR_CONTAINER
        ]
        return (
            self.problem.PRIMARY_CONTAINER in regular_names
            and self.problem.SIDECAR_CONTAINER not in regular_names
            and len(native_sidecars) == 1
            and native_sidecars[0].restart_policy == "Always"
        )

    def _fix_description(self, cronjob) -> str:
        if cronjob is None:
            return "CronJob deleted (rejected)"
        jts = cronjob.spec.job_template.spec
        pod_spec = jts.template.spec
        for init_container in pod_spec.init_containers or []:
            if (
                init_container.name == self.problem.SIDECAR_CONTAINER
                and getattr(init_container, "restart_policy", None) == "Always"
            ):
                return f"native sidecar ({init_container.name})"
        if (jts.active_deadline_seconds or 0) > 0:
            return f"activeDeadlineSeconds={jts.active_deadline_seconds} (rejected)"
        if len(pod_spec.containers or []) <= 1:
            return "sidecar removed (rejected)"
        return "unmitigated"

    def _run_controlled_job(self, cronjob, namespace: str) -> bool:
        """Create one Job from the current CronJob template and require success."""
        job_name = f"audit-log-archiver-run-{time.time_ns()}"[:63]
        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=namespace,
                labels={
                    "app.kubernetes.io/name": self.problem.cronjob_name,
                    "app.kubernetes.io/managed-by": self.problem.cronjob_name,
                },
            ),
            spec=copy.deepcopy(cronjob.spec.job_template.spec),
        )

        created = False
        deadline = time.monotonic() + _BEHAVIOR_PROOF_TIMEOUT_S
        try:
            self.batch_v1.create_namespaced_job(namespace=namespace, body=job)
            created = True
            while time.monotonic() < deadline:
                current = self.batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
                if (current.status.succeeded or 0) >= 1:
                    print(f"  [behavior-check] Job '{job_name}' reached Complete")
                    return True
                if (current.status.failed or 0) >= 1:
                    print(f"  [behavior-check] Job '{job_name}' failed")
                    return False
                time.sleep(_BEHAVIOR_PROOF_POLL_INTERVAL_S)
            return False
        except ApiException as error:
            print(f"  [behavior-check] Could not run Job '{job_name}': {error}")
            return False
        finally:
            if created:
                with contextlib.suppress(ApiException):
                    self.batch_v1.delete_namespaced_job(
                        name=job_name,
                        namespace=namespace,
                        propagation_policy="Foreground",
                    )

    def _active_jobs(self, cronjob_name, namespace) -> list:
        """Return active Jobs owned by the target CronJob."""
        jobs = self.batch_v1.list_namespaced_job(namespace=namespace)
        return [job for job in jobs.items if self._owned_by_cronjob(job, cronjob_name) and (job.status.active or 0) > 0]

    @staticmethod
    def _owned_by_cronjob(job, cronjob_name: str) -> bool:
        return any(ref.kind == "CronJob" and ref.name == cronjob_name for ref in job.metadata.owner_references or [])

    def _unhealthy_deployment(self, namespace):
        """Return the name of the first under-replicated Deployment, or None."""
        deployments = self.apps_v1.list_namespaced_deployment(namespace=namespace)
        for dep in deployments.items:
            desired = dep.spec.replicas or 1
            ready = dep.status.ready_replicas or 0
            if ready < desired:
                return dep.metadata.name
        return None

    @staticmethod
    def _fail(reason: str) -> dict:
        print(f"❌ {reason}")
        return {"success": False, "reason": reason}
