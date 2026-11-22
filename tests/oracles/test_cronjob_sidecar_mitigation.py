from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles import cronjob_sidecar_mitigation as module
from sregym.conductor.oracles.cronjob_sidecar_mitigation import CronJobSidecarBlocksCompletionMitigationOracle
from sregym.conductor.problems.cronjob_sidecar_blocks_completion import CronJobSidecarBlocksCompletionHotelReservation

NS = "hotel-reservation"


def _cronjob(native=True):
    problem = object.__new__(CronJobSidecarBlocksCompletionHotelReservation)
    problem.namespace = NS
    body = problem._build_cronjob_body_with_native_sidecar() if native else problem._build_cronjob_body()
    return client.ApiClient()._ApiClient__deserialize(body, "V1CronJob")


def _job(name="controlled", uid="job-uid", active=0, condition="Complete"):
    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name,
            uid=uid,
            owner_references=[
                client.V1OwnerReference(
                    api_version="batch/v1", kind="CronJob", name="audit-log-archiver", uid="cron-uid"
                )
            ],
        ),
        status=client.V1JobStatus(
            active=active,
            succeeded=1 if condition == "Complete" else 0,
            conditions=[client.V1JobCondition(type=condition, status="True")] if condition else [],
        ),
    )


def _pod(uid="job-uid", phase="Succeeded", exit_code=0):
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="controlled-pod",
            owner_references=[client.V1OwnerReference(api_version="batch/v1", kind="Job", name="controlled", uid=uid)],
        ),
        status=client.V1PodStatus(
            phase=phase,
            container_statuses=[
                client.V1ContainerStatus(
                    name="archiver",
                    image="busybox:1.36",
                    image_id="sha256:test",
                    ready=False,
                    restart_count=0,
                    state=client.V1ContainerState(terminated=client.V1ContainerStateTerminated(exit_code=exit_code)),
                )
            ],
        ),
    )


def _oracle():
    problem = SimpleNamespace(
        cronjob_name="audit-log-archiver",
        namespace=NS,
        PRIMARY_CONTAINER="archiver",
        SIDECAR_CONTAINER="fluent-bit-sidecar",
        SIDECAR_PORT=24224,
        WORKLOAD_IMAGE="busybox:1.36",
        SCHEDULE="* * * * *",
        kubectl=Mock(),
    )
    oracle = CronJobSidecarBlocksCompletionMitigationOracle(problem)
    oracle.batch_v1 = Mock()
    oracle.core_v1 = Mock()
    oracle.batch_v1.list_namespaced_job.return_value = SimpleNamespace(items=[])
    oracle.batch_v1.create_namespaced_job.return_value = _job()

    def current():
        job = _job()
        created = oracle.batch_v1.create_namespaced_job.call_args.kwargs["body"]
        job.spec = created.spec
        for pod in oracle.core_v1.list_namespaced_pod.return_value.items:
            pod.spec = created.spec.template.spec
        return job

    oracle.batch_v1.read_namespaced_job.side_effect = lambda **kwargs: current()
    oracle.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod()])
    oracle._wait_for_rollouts = Mock()
    oracle._evaluate_current_state = Mock(return_value={"success": True})
    oracle._delete_controlled_job = Mock(return_value=True)

    def logs(*args, container, **kwargs):
        created = oracle.batch_v1.create_namespaced_job.call_args.kwargs["body"]
        primary = next(c for c in created.spec.template.spec.containers if c.name == "archiver")
        record = next(e.value for e in primary.env if e.name == "AUDIT_ARCHIVE_RECORD")
        if "forwarding-check:" in primary.command[-1]:
            marker = primary.command[-1].split("'forwarding-check:", 1)[1].split("'", 1)[0]
            return f"forwarding-check:{marker}\n"
        return f"archiver: upload complete (0 bytes archived); record={record}\n"

    oracle.core_v1.read_namespaced_pod_log.side_effect = logs
    return oracle


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("deadline", [None, 30, 600])
def test_accepts_both_role_layouts_and_does_not_confuse_deadline_with_repair(native, deadline):
    cj = _cronjob(native)
    cj.spec.job_template.spec.active_deadline_seconds = deadline
    assert _oracle()._workload_present(cj)


@pytest.mark.parametrize("change", ["suspend", "schedule", "primary", "sidecar", "duplicate"])
def test_rejects_missing_or_disabled_workload(change):
    cj = _cronjob()
    spec = cj.spec.job_template.spec.template.spec
    if change == "suspend":
        cj.spec.suspend = True
    elif change == "schedule":
        cj.spec.schedule = "0 0 * * *"
    elif change == "primary":
        spec.containers = [client.V1Container(name="unrelated")]
    elif change == "sidecar":
        spec.init_containers = [client.V1Container(name="unrelated", restart_policy="Always")]
    else:
        spec.containers.append(client.V1Container(name="fluent-bit-sidecar"))
    assert not _oracle()._workload_present(cj)


@pytest.mark.parametrize("native", [True, False])
def test_runtime_proof_preserves_template_and_private_record_only_reaches_primary(native):
    cj = _cronjob(native)
    original = cj.to_dict()
    oracle = _oracle()
    assert oracle._run_controlled_job(cj, NS) == {"success": True}
    created = oracle.batch_v1.create_namespaced_job.call_args.kwargs["body"]
    assert cj.to_dict() == original
    assert created.spec is not cj.spec.job_template.spec
    assert created.metadata.name.startswith("audit-log-archiver-run-")
    containers = created.spec.template.spec.containers + (created.spec.template.spec.init_containers or [])
    for container in containers:
        records = [e for e in container.env or [] if e.name == "AUDIT_ARCHIVE_RECORD"]
        assert len(records) == (1 if container.name == "archiver" else 0)
    assert oracle._delete_controlled_job.call_count == 2


def test_repeated_proof_uses_fresh_records_and_replaces_existing_input():
    oracle = _oracle()
    cj = _cronjob()
    cj.spec.job_template.spec.template.spec.containers[0].env = [
        client.V1EnvVar(name="AUDIT_ARCHIVE_RECORD", value="stale"),
        client.V1EnvVar(name="OTHER_SETTING", value="kept"),
    ]
    for _ in range(2):
        assert oracle._run_controlled_job(cj, NS)["success"]
    jobs = [call.kwargs["body"] for call in oracle.batch_v1.create_namespaced_job.call_args_list]
    records = [{e.name: e.value for e in job.spec.template.spec.containers[0].env} for job in jobs[::2]]
    assert records[0]["AUDIT_ARCHIVE_RECORD"] != records[1]["AUDIT_ARCHIVE_RECORD"]
    assert records[0]["OTHER_SETTING"] == "kept"


def test_static_fabricated_logs_do_not_prove_fresh_work():
    oracle = _oracle()
    oracle.core_v1.read_namespaced_pod_log.side_effect = lambda *a, **kw: "archiver: upload complete; record=stale\n"
    result = oracle._run_controlled_job(_cronjob(), NS)
    assert result["reason"] == "audit_record_not_forwarded"


@pytest.mark.parametrize("change", ["wrong-owner", "running", "failed-primary", "missing-primary", "no-pods"])
def test_success_flag_without_matching_executed_pod_is_insufficient(change):
    oracle = _oracle()
    pod = _pod(
        uid="other" if change == "wrong-owner" else "job-uid",
        phase="Running" if change == "running" else "Succeeded",
        exit_code=1 if change == "failed-primary" else 0,
    )
    if change == "missing-primary":
        pod.status.container_statuses = []
    oracle.core_v1.list_namespaced_pod.return_value.items = [] if change == "no-pods" else [pod]
    assert oracle._run_controlled_job(_cronjob(), NS)["reason"] == "audit_record_not_forwarded"


def test_failed_job_is_rejected_and_cleaned():
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_job.side_effect = None
    oracle.batch_v1.read_namespaced_job.return_value = _job(condition="Failed")
    assert oracle._run_controlled_job(_cronjob(), NS)["reason"] == "fresh_job_did_not_complete"
    oracle._delete_controlled_job.assert_called_once()


def test_failed_pod_retry_is_allowed_until_job_is_terminal(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    oracle = _oracle()
    retrying = _job(condition=None)
    retrying.status.failed = 1
    current = oracle.batch_v1.read_namespaced_job.side_effect
    oracle.batch_v1.read_namespaced_job.side_effect = lambda **kwargs: (
        retrying if oracle.batch_v1.read_namespaced_job.call_count == 1 else current(**kwargs)
    )
    assert oracle._run_controlled_job(_cronjob(), NS)["success"]


@pytest.mark.parametrize(
    "method", ["create_namespaced_job", "read_namespaced_job", "list_namespaced_pod", "read_namespaced_pod_log"]
)
def test_api_errors_never_pass(method):
    oracle = _oracle()
    api = oracle.batch_v1 if "job" in method else oracle.core_v1
    getattr(api, method).side_effect = ApiException(status=403)
    assert not oracle._run_controlled_job(_cronjob(), NS)["success"]
    assert oracle._delete_controlled_job.call_count == (method != "create_namespaced_job")


def test_replaced_job_uid_is_rejected():
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_job.side_effect = None
    oracle.batch_v1.read_namespaced_job.return_value = _job(uid="replacement")
    assert not oracle._run_controlled_job(_cronjob(), NS)["success"]


def test_timeout_is_rejected_and_cleaned(monkeypatch):
    oracle = _oracle()
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 91]))
    assert not oracle._run_controlled_job(_cronjob(), NS)["success"]
    oracle._delete_controlled_job.assert_called_once()


@pytest.mark.parametrize("native", [True, False])
def test_evaluate_uses_runtime_not_layout(native):
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_cron_job.return_value = _cronjob(native)
    assert oracle.evaluate() == {"success": True}
    oracle._evaluate_current_state.assert_called_once()


def test_pending_old_jobs_cannot_be_hidden_by_current_success(monkeypatch):
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_cron_job.return_value = _cronjob()
    oracle.batch_v1.list_namespaced_job.return_value = SimpleNamespace(items=[_job(active=1, condition=None)])
    oracle._run_controlled_job = Mock(return_value={"success": True})
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 91]))
    assert oracle.evaluate()["reason"] == "stale_jobs_not_cleaned_up"


def test_pending_safe_cooperative_job_can_finish_during_proof():
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_cron_job.return_value = _cronjob(False)
    oracle.batch_v1.list_namespaced_job.side_effect = [
        SimpleNamespace(items=[_job(active=1, condition=None)]),
        SimpleNamespace(items=[]),
    ]
    assert oracle.evaluate()["success"]


def test_unrelated_active_job_is_not_counted_as_archiver():
    oracle = _oracle()
    job = _job(active=1, condition=None)
    job.metadata.owner_references[0].name = "unrelated"
    oracle.batch_v1.list_namespaced_job.return_value.items = [job]
    assert oracle._active_jobs("audit-log-archiver", NS) == []


def test_application_failure_is_preserved():
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_cron_job.return_value = _cronjob()
    oracle._evaluate_current_state.return_value = {"success": False, "reason": "required_resource_missing"}
    assert oracle.evaluate()["reason"] == "required_resource_missing"


def test_deleted_cronjob_is_rejected():
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_cron_job.side_effect = ApiException(status=404)
    assert oracle.evaluate()["reason"] == "cronjob_deleted"


def test_archiver_checks_echo_and_native_recovery_preserves_forwarder():
    regular = _cronjob(False).spec.job_template.spec.template.spec
    assert "AUDIT_ARCHIVE_RECORD" in regular.containers[0].command[-1]
    assert '[ "$reply" = "$record" ]' in regular.containers[0].command[-1]
    assert "-e cat" in regular.containers[1].command[-1]
    native = _cronjob().spec.job_template.spec.template.spec
    assert native.init_containers[0].restart_policy == "Always"


def test_grading_budget_covers_both_proofs_cleanup_and_rollout_settling():
    assert CronJobSidecarBlocksCompletionMitigationOracle.evaluation_timeout_seconds >= 60 + 120 + 2 * (90 + 60)


def test_normal_completion_is_not_modified_by_the_forwarding_probe():
    import copy

    oracle = _oracle()
    captured = []
    oracle.batch_v1.create_namespaced_job.side_effect = lambda **kw: (
        captured.append(copy.deepcopy(kw["body"])) or _job()
    )
    cj = _cronjob(False)
    original_command = cj.spec.job_template.spec.template.spec.containers[0].command
    assert oracle._run_controlled_job(cj, NS)["success"]
    assert len(captured) == 2
    normal, probe = captured
    assert normal.spec.template.spec.containers[0].command == original_command
    assert probe.spec.template.spec.containers[0].command != original_command
    assert normal.spec.template.spec.containers[1] == probe.spec.template.spec.containers[1]


@pytest.mark.parametrize("status,expected", [(404, True), (403, False)])
def test_cleanup_waits_for_job_removal(status, expected):
    oracle = _oracle()
    oracle.batch_v1.read_namespaced_job.side_effect = ApiException(status=status)
    assert type(oracle)._delete_controlled_job(oracle, "test-job", NS) is expected


def test_cleanup_timeout_is_not_success(monkeypatch):
    oracle = _oracle()
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 61]))
    assert type(oracle)._delete_controlled_job(oracle, "test-job", NS) is False


def test_cleanup_failure_cannot_be_graded_as_success():
    oracle = _oracle()
    oracle._delete_controlled_job.return_value = False
    assert oracle._run_controlled_job(_cronjob(), NS)["reason"] == "probe_cleanup_failed"


@pytest.mark.parametrize("owned", [True, False])
def test_only_real_target_job_pods_are_exempt_from_application_readiness(owned):
    oracle = _oracle()
    job = _job(uid="owned-job")
    oracle.batch_v1.list_namespaced_job.return_value.items = [job]
    pod = _pod(uid="owned-job" if owned else "unrelated", phase="Pending")
    result = oracle.pods_unready([pod])
    assert result is None if owned else result["reason"] == "pods_not_ready"


def test_forwarding_probe_accepts_cooperative_sidecar_still_waiting_for_shutdown():
    oracle = _oracle()
    current = oracle.batch_v1.read_namespaced_job.side_effect

    def read(**kwargs):
        job = current(**kwargs)
        if oracle.batch_v1.create_namespaced_job.call_count == 2:
            job.status.conditions = []
            oracle.core_v1.list_namespaced_pod.return_value.items[0].status.phase = "Running"
        return job

    oracle.batch_v1.read_namespaced_job.side_effect = read
    assert oracle._run_controlled_job(_cronjob(False), NS)["success"]


def test_admission_cannot_replace_the_probe_with_a_noop():
    import copy

    oracle = _oracle()
    current = oracle.batch_v1.read_namespaced_job.side_effect

    def read(**kwargs):
        job = current(**kwargs)
        pod = oracle.core_v1.list_namespaced_pod.return_value.items[0]
        pod.spec = copy.deepcopy(pod.spec)
        pod.spec.containers[0].command = ["sh", "-c", "exit 0"]
        return job

    oracle.batch_v1.read_namespaced_job.side_effect = read
    assert oracle._run_controlled_job(_cronjob(), NS)["reason"] == "audit_record_not_forwarded"
