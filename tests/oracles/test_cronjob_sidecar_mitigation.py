from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.cronjob_sidecar_mitigation import (
    CronJobSidecarBlocksCompletionMitigationOracle,
)
from sregym.conductor.problems.cronjob_sidecar_blocks_completion import (
    CronJobSidecarBlocksCompletionHotelReservation,
)


def _container(name, restart_policy=None):
    return SimpleNamespace(name=name, restart_policy=restart_policy)


def _job_spec(
    *,
    native_sidecar=True,
    dummy_native=False,
    regular_sidecar=False,
    primary=True,
    job_deadline=None,
    pod_deadline=None,
):
    containers = []
    if primary:
        containers.append(_container("archiver"))
    if regular_sidecar:
        containers.append(_container("fluent-bit-sidecar"))

    init_containers = []
    if native_sidecar:
        init_containers.append(_container("fluent-bit-sidecar", "Always"))
    if dummy_native:
        init_containers.append(_container("telemetry-bootstrap", "Always"))

    pod_spec = SimpleNamespace(
        containers=containers,
        init_containers=init_containers,
        active_deadline_seconds=pod_deadline,
    )
    return SimpleNamespace(
        active_deadline_seconds=job_deadline,
        template=SimpleNamespace(spec=pod_spec),
    )


def _cronjob(*, job_spec=None, suspend=False, schedule="* * * * *"):
    return SimpleNamespace(
        spec=SimpleNamespace(
            suspend=suspend,
            schedule=schedule,
            job_template=SimpleNamespace(spec=job_spec or _job_spec()),
        )
    )


def _owned_job(name, job_spec, *, active=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            owner_references=[SimpleNamespace(kind="CronJob", name="audit-log-archiver")],
        ),
        spec=job_spec,
        status=SimpleNamespace(active=active, succeeded=0),
    )


class _BatchV1:
    def __init__(self, *, jobs=None, controlled_status=None):
        self.jobs = jobs or []
        self.controlled_status = controlled_status or SimpleNamespace(succeeded=1, failed=0)
        self.created = []
        self.deleted = []

    def list_namespaced_job(self, namespace):
        return SimpleNamespace(items=self.jobs)

    def create_namespaced_job(self, namespace, body):
        self.created.append((namespace, body))

    def read_namespaced_job(self, name, namespace):
        return SimpleNamespace(status=self.controlled_status)

    def delete_namespaced_job(self, name, namespace, propagation_policy):
        self.deleted.append((name, namespace, propagation_policy))


def _oracle(batch_v1=None):
    problem = SimpleNamespace(
        cronjob_name="audit-log-archiver",
        PRIMARY_CONTAINER="archiver",
        SIDECAR_CONTAINER="fluent-bit-sidecar",
        SCHEDULE="* * * * *",
    )
    oracle = CronJobSidecarBlocksCompletionMitigationOracle(problem)
    oracle.batch_v1 = batch_v1 or _BatchV1()
    return oracle


def test_accepts_named_native_sidecar_with_primary_and_no_deadline():
    assert _oracle()._is_spec_fixed(_cronjob()) is True


def test_rejects_unrelated_restartable_init_when_real_sidecar_is_removed():
    spec = _job_spec(native_sidecar=False, dummy_native=True)

    assert _oracle()._is_spec_fixed(_cronjob(job_spec=spec)) is False


def test_rejects_real_sidecar_left_in_regular_containers():
    spec = _job_spec(dummy_native=True, regular_sidecar=True)

    assert _oracle()._is_spec_fixed(_cronjob(job_spec=spec)) is False


@pytest.mark.parametrize(
    "job_spec",
    [
        _job_spec(job_deadline=30),
        _job_spec(pod_deadline=30),
    ],
)
def test_rejects_active_deadline_workarounds(job_spec):
    assert _oracle()._is_spec_fixed(_cronjob(job_spec=job_spec)) is False


def test_rejects_suspended_or_rescheduled_cronjob():
    assert _oracle()._is_spec_fixed(_cronjob(suspend=True)) is False
    assert _oracle()._is_spec_fixed(_cronjob(schedule="0 0 * * *")) is False


def test_identifies_active_job_from_old_regular_sidecar_template_as_unsafe():
    stale = _owned_job(
        "audit-log-archiver-old",
        _job_spec(native_sidecar=False, regular_sidecar=True),
    )
    current = _owned_job("audit-log-archiver-new", _job_spec())
    batch_v1 = _BatchV1(jobs=[stale, current])
    oracle = _oracle(batch_v1)

    active = oracle._active_jobs("audit-log-archiver", "hotel-reservation")

    assert [job.metadata.name for job in active] == [
        "audit-log-archiver-old",
        "audit-log-archiver-new",
    ]
    assert oracle._job_spec_is_safe(stale.spec) is False
    assert oracle._job_spec_is_safe(current.spec) is True


def test_runtime_proof_creates_and_deletes_a_fresh_job_from_current_template():
    batch_v1 = _BatchV1()
    oracle = _oracle(batch_v1)
    cronjob = _cronjob()

    assert oracle._run_controlled_job(cronjob, "hotel-reservation") is True

    controlled = batch_v1.created[0][1]
    assert controlled.metadata.name.startswith("audit-log-archiver-run-")
    assert controlled.spec is not cronjob.spec.job_template.spec
    assert len(batch_v1.deleted) == 1


def test_runtime_proof_rejects_failed_current_template_and_cleans_job():
    batch_v1 = _BatchV1(controlled_status=SimpleNamespace(succeeded=0, failed=1))
    oracle = _oracle(batch_v1)

    assert oracle._run_controlled_job(_cronjob(), "hotel-reservation") is False
    assert len(batch_v1.deleted) == 1


def test_archiver_requires_live_sidecar_and_native_recovery_preserves_it():
    problem = object.__new__(CronJobSidecarBlocksCompletionHotelReservation)
    problem.namespace = "hotel-reservation"

    broken = problem._build_cronjob_body()
    primary = broken["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert "nc -w 2 127.0.0.1 24224" in primary["command"][-1]

    recovered = problem._build_cronjob_body_with_native_sidecar()
    pod_spec = recovered["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert [container["name"] for container in pod_spec["containers"]] == ["archiver"]
    assert pod_spec["initContainers"][0]["name"] == "fluent-bit-sidecar"
    assert pod_spec["initContainers"][0]["restartPolicy"] == "Always"
