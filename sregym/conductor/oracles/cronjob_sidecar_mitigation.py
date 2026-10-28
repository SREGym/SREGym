"""Check archival, forwarding, and completion without prescribing a sidecar layout.

One Job proves completion of the repaired template with a fresh audit record.
A separate probe checks the forwarding socket without changing that workload's
shutdown protocol. Native and cooperative regular sidecars share this contract.
"""

import copy
import time
import uuid

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle

_BEHAVIOR_PROOF_TIMEOUT_S = 90
_BEHAVIOR_PROOF_POLL_INTERVAL_S = 2
_RECORD_ENV = "AUDIT_ARCHIVE_RECORD"


class CronJobSidecarBlocksCompletionMitigationOracle(MitigationOracle):
    """Require a functioning scheduled workload, bounded Jobs, and a healthy app."""

    importance = 1.0
    # Two bounded Job checks, their cascading cleanup, and rollout settling can
    # exceed the runner's default 300s grading window. Agent time is unchanged.
    evaluation_timeout_seconds = 600
    FAILURE_CLASSES = {
        "cronjob_deleted": FailureClass.AGENT_ERROR,
        "stale_jobs_not_cleaned_up": FailureClass.AGENT_ERROR,
        "fresh_job_did_not_complete": FailureClass.AMBIGUOUS,
        "audit_record_not_forwarded": FailureClass.AGENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        self.batch_v1 = client.BatchV1Api()
        self.core_v1 = client.CoreV1Api()

    def evaluate(self, *args, **kwargs) -> dict:
        print("== CronJob Sidecar Mitigation Evaluation ==")
        namespace = self.problem.namespace
        name = self.problem.cronjob_name
        self._wait_for_rollouts(self.problem.kubectl, namespace)

        cronjob = self._get_cronjob(name, namespace)
        if cronjob is None:
            return self.fail("cronjob_deleted", cronjob=name)
        if not self._workload_present(cronjob):
            return self.fail("fault_still_present", cronjob=name)

        # Observe existing Jobs, not their manifest shape. An ordinary sidecar
        # may shut down cooperatively; a native-looking template may still hang.
        pending = {job.metadata.uid: job.metadata.name for job in self._active_jobs(name, namespace)}
        deadline = time.monotonic() + _BEHAVIOR_PROOF_TIMEOUT_S
        proof = self._run_controlled_job(cronjob, namespace)
        if not proof["success"]:
            return proof
        while pending:
            active = {job.metadata.uid: job.metadata.name for job in self._active_jobs(name, namespace)}
            pending = {uid: job for uid, job in pending.items() if uid in active}
            if not pending:
                break
            if time.monotonic() >= deadline:
                return self.fail("stale_jobs_not_cleaned_up", jobs=sorted(pending.values()))
            time.sleep(_BEHAVIOR_PROOF_POLL_INTERVAL_S)

        health = self._evaluate_current_state()
        if not health["success"]:
            return health
        print("✅ Fresh audit record forwarded; Job completed; no old active Jobs; application healthy")
        return {"success": True}

    def _get_cronjob(self, name, namespace):
        try:
            return self.batch_v1.read_namespaced_cron_job(name=name, namespace=namespace)
        except ApiException as error:
            if error.status == 404:
                return None
            raise

    def _workload_present(self, cronjob) -> bool:
        """Keep workload roles and schedule; prove lifecycle at runtime.

        A deadline is not itself a repair or a defect. A healthy bounded Job may
        have one, but a Job killed at its deadline cannot satisfy the proof.
        """
        if cronjob.spec.suspend or cronjob.spec.schedule != self.problem.SCHEDULE:
            return False
        spec = cronjob.spec.job_template.spec.template.spec
        regular = [container.name for container in spec.containers or []]
        all_names = regular + [container.name for container in spec.init_containers or []]
        return (
            regular.count(self.problem.PRIMARY_CONTAINER) == 1 and all_names.count(self.problem.SIDECAR_CONTAINER) == 1
        )

    def _run_controlled_job(self, cronjob, namespace: str) -> dict:
        """Prove normal completion, then test forwarding in a separate Job.

        Keep the real template's command and shutdown protocol intact. A second
        Job checks the forwarding socket independently, so a one-shot listener
        does not receive an extra record during the completion test.
        """
        record = "audit-" + uuid.uuid4().hex
        spec = copy.deepcopy(cronjob.spec.job_template.spec)
        primary = next(c for c in spec.template.spec.containers if c.name == self.problem.PRIMARY_CONTAINER)
        primary.env = [variable for variable in primary.env or [] if variable.name != _RECORD_ENV]
        primary.env.append(client.V1EnvVar(name=_RECORD_ENV, value=record))
        result = self._run_job(spec, namespace, record, probe=False)
        if not result["success"]:
            return result

        # Only this disposable probe replaces the primary. Its sidecar need not
        # stop cooperatively: the original template already proved completion.
        record = "audit-" + uuid.uuid4().hex
        primary.image = self.problem.WORKLOAD_IMAGE
        primary.command = [
            "sh",
            "-c",
            "set -eu\n"
            "attempt=0\n"
            f'until reply=$(printf "%s\\n" "{record}" | nc -w 2 127.0.0.1 {self.problem.SIDECAR_PORT}) '
            f'&& [ "$reply" = "{record}" ]; do\n'
            '  attempt=$((attempt + 1)); [ "$attempt" -lt 10 ] || exit 1\n'
            "  sleep 1\n"
            "done\n"
            f"printf '%s\\n' 'forwarding-check:{record}'\n",
        ]
        primary.args = None
        return self._run_job(spec, namespace, record, probe=True)

    def _run_job(self, spec, namespace: str, record: str, *, probe: bool) -> dict:
        job_name = "audit-log-archiver-run-" + uuid.uuid4().hex[:16]
        job = client.V1Job(metadata=client.V1ObjectMeta(name=job_name, namespace=namespace), spec=spec)
        primary = next(c for c in spec.template.spec.containers if c.name == self.problem.PRIMARY_CONTAINER)
        expected_command = copy.deepcopy((primary.command, primary.args))
        created = None
        result = self.fail("fresh_job_did_not_complete", job=job_name, timeout_seconds=_BEHAVIOR_PROOF_TIMEOUT_S)
        try:
            created = self.batch_v1.create_namespaced_job(namespace=namespace, body=job)
            deadline = time.monotonic() + _BEHAVIOR_PROOF_TIMEOUT_S
            while time.monotonic() < deadline:
                current = self.batch_v1.read_namespaced_job(name=job_name, namespace=namespace)
                if current.metadata.uid != created.metadata.uid:
                    break
                conditions = {
                    condition.type for condition in current.status.conditions or [] if condition.status == "True"
                }
                if probe or "Complete" in conditions:
                    if self._record_forwarded(current, namespace, record, expected_command, probe=probe):
                        print(
                            f"  [behavior-check] Job '{job_name}': {'forwarding verified' if probe else 'archival completed'}"
                        )
                        result = {"success": True}
                        break
                    if "Complete" in conditions:
                        result = self.fail("audit_record_not_forwarded", job=job_name)
                        break
                if "Failed" in conditions:
                    break
                time.sleep(_BEHAVIOR_PROOF_POLL_INTERVAL_S)
        except ApiException as error:
            print(f"  [behavior-check] Could not verify Job '{job_name}': {error}")
        finally:
            if created is not None and not self._delete_controlled_job(job_name, namespace):
                result = self.fail("probe_cleanup_failed", job=job_name)
        return result

    def _record_forwarded(self, job, namespace: str, record: str, expected_command, *, probe: bool) -> bool:
        pods = self.core_v1.list_namespaced_pod(
            namespace=namespace, label_selector=f"batch.kubernetes.io/job-name={job.metadata.name}"
        )
        for pod in pods.items:
            if (not probe and pod.status.phase != "Succeeded") or not any(
                ref.kind == "Job" and ref.uid == job.metadata.uid for ref in pod.metadata.owner_references or []
            ):
                continue
            statuses = {status.name: status for status in pod.status.container_statuses or []}
            primary = statuses.get(self.problem.PRIMARY_CONTAINER)
            if primary is None or primary.state.terminated is None or primary.state.terminated.exit_code != 0:
                continue
            actual = next((c for c in pod.spec.containers if c.name == self.problem.PRIMARY_CONTAINER), None)
            command, arguments = expected_command
            if actual is None or actual.command != command or (actual.args or []) != (arguments or []):
                continue
            logs = self.core_v1.read_namespaced_pod_log(
                pod.metadata.name, namespace, container=self.problem.PRIMARY_CONTAINER, _request_timeout=10
            )
            if probe:
                return f"forwarding-check:{record}" in logs.splitlines()
            if any("archiver: upload complete" in line and f"record={record}" in line for line in logs.splitlines()):
                return True
        return False

    def _delete_controlled_job(self, name, namespace: str) -> bool:
        """Wait for cascading deletion before checking the application's pods."""
        try:
            self.batch_v1.delete_namespaced_job(name=name, namespace=namespace, propagation_policy="Foreground")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                self.batch_v1.read_namespaced_job(name=name, namespace=namespace)
                time.sleep(_BEHAVIOR_PROOF_POLL_INTERVAL_S)
        except ApiException as error:
            if error.status == 404:
                return True
            print(f"  [behavior-check] Could not clean Job '{name}': {error}")
        return False

    def _active_jobs(self, cronjob_name, namespace) -> list:
        jobs = self.batch_v1.list_namespaced_job(namespace=namespace)
        return [job for job in jobs.items if self._owned_by_cronjob(job, cronjob_name) and (job.status.active or 0) > 0]

    @staticmethod
    def _owned_by_cronjob(job, cronjob_name: str) -> bool:
        return any(ref.kind == "CronJob" and ref.name == cronjob_name for ref in job.metadata.owner_references or [])

    def pods_unready(self, pods, **detail):
        # A schedule boundary can start another short Job after our observation.
        # Its primary's normal exit is not application unhealthiness.
        jobs = self.batch_v1.list_namespaced_job(namespace=self.problem.namespace)
        target_uids = {job.metadata.uid for job in jobs.items if self._owned_by_cronjob(job, self.problem.cronjob_name)}
        app_pods = [
            pod
            for pod in pods
            if not any(ref.kind == "Job" and ref.uid in target_uids for ref in pod.metadata.owner_references or [])
        ]
        return super().pods_unready(app_pods, **detail)
