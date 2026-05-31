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
2. **Active Jobs are bounded.** No more than ``MAX_ACTIVE_JOBS`` Jobs from this
   CronJob are currently in the ``active`` phase. (Accumulation has been
   reversed.)
3. **App still healthy.** Every Deployment in the namespace reports
   ``ready_replicas == spec.replicas``. We check Deployment status directly
   rather than walking pods so ``Succeeded`` Job pods don't produce false
   negatives.
4. **Fix actually works at runtime.** At least one Job owned by the CronJob
   has reached ``status.succeeded >= 1``. This proves the agent's patch is
   not just plausibly shaped, it really makes the Job complete.
"""

import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.base import Oracle

_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 5
_MAX_ACTIVE_JOBS = 2  # allow the most-recent in-flight Job(s); reject accumulation

# How long to wait for at least one Job to reach Complete after the spec is
# fixed. With ``schedule: * * * * *`` the next Job spawns within ~60s. With a
# correctly-configured native sidecar, the Job's pod goes to ``Succeeded``
# within one terminationGracePeriodSeconds window (default 30s) after the
# primary exits. 150s leaves headroom for the schedule boundary.
_BEHAVIOR_PROOF_TIMEOUT_S = 150
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
    def evaluate(self) -> dict:
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

        # 2. Are active Jobs bounded?
        n_active = self._count_active_jobs(cronjob_name, namespace)
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

        # 4. Does the fix actually work? Wait for at least one Job to
        # complete. With the native sidecar pattern the kubelet SIGTERMs the
        # restartable init container after the primary exits, and the pod
        # goes to Succeeded; if the patch is shaped wrong (e.g. sidecar
        # still in containers, or restartPolicy not set), no Job will ever
        # reach Complete and this check fails.
        print(f"Waiting up to {_BEHAVIOR_PROOF_TIMEOUT_S}s for a Job to reach Complete...")
        if not self._wait_for_completed_job(cronjob_name, namespace):
            return self._fail(
                f"Spec looks fixed but no Job owned by '{cronjob_name}' has reached "
                f"status.succeeded within {_BEHAVIOR_PROOF_TIMEOUT_S}s. Check that the "
                "sidecar is in initContainers with restartPolicy=Always (not just moved), "
                "and that no other container is also blocking termination."
            )

        print(
            f"✅ Spec fixed ({self._fix_description(cj)}); "
            f"{n_active} active Jobs ≤ {_MAX_ACTIVE_JOBS}; app healthy; "
            "at least one Job reached Complete (fix verified at runtime)"
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

        The accepted post-fix shape: at least one container in
        ``spec.jobTemplate.spec.template.spec.initContainers`` has
        ``restartPolicy: Always``. With the ``SidecarContainers`` feature gate
        (beta-default-on in 1.29, GA in 1.33), the kubelet treats such an init
        container as a sidecar and sends it SIGTERM after the regular
        containers exit, allowing the Pod to reach ``Succeeded`` and the Job
        to reach ``Complete``.

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

        pod_spec = cronjob.spec.job_template.spec.template.spec

        return any(getattr(ic, "restart_policy", None) == "Always" for ic in pod_spec.init_containers or [])

    def _fix_description(self, cronjob) -> str:
        if cronjob is None:
            return "CronJob deleted (rejected)"
        jts = cronjob.spec.job_template.spec
        pod_spec = jts.template.spec
        for ic in pod_spec.init_containers or []:
            if getattr(ic, "restart_policy", None) == "Always":
                return f"native sidecar ({ic.name})"
        if (jts.active_deadline_seconds or 0) > 0:
            return f"activeDeadlineSeconds={jts.active_deadline_seconds} (rejected)"
        if len(pod_spec.containers or []) <= 1:
            return "sidecar removed (rejected)"
        return "unmitigated"

    def _wait_for_completed_job(self, cronjob_name, namespace) -> bool:
        """Poll until at least one Job owned by the CronJob reports
        ``status.succeeded >= 1``, or the timeout expires."""
        deadline = time.monotonic() + _BEHAVIOR_PROOF_TIMEOUT_S
        last_state = None
        while time.monotonic() < deadline:
            jobs = self.batch_v1.list_namespaced_job(namespace=namespace)
            owned = [j for j in jobs.items if self._owned_by_cronjob(j, cronjob_name)]
            n_succeeded = sum(1 for j in owned if (j.status.succeeded or 0) >= 1)
            n_active = sum(1 for j in owned if (j.status.active or 0) > 0)
            state = (len(owned), n_active, n_succeeded)
            if state != last_state:
                print(f"  [behavior-check] owned={state[0]} active={state[1]} succeeded={state[2]}")
                last_state = state
            if n_succeeded >= 1:
                return True
            time.sleep(_BEHAVIOR_PROOF_POLL_INTERVAL_S)
        return False

    def _count_active_jobs(self, cronjob_name, namespace) -> int:
        """Count Jobs owned by the CronJob whose ``.status.active`` is non-zero."""
        jobs = self.batch_v1.list_namespaced_job(namespace=namespace)
        n = 0
        for j in jobs.items:
            if not self._owned_by_cronjob(j, cronjob_name):
                continue
            if (j.status.active or 0) > 0:
                n += 1
        return n

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
