from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.finalizer_deadlock_controller_mitigation import (
    FinalizerDeadlockControllerMitigationOracle,
)
from sregym.conductor.problems.finalizer_deadlock_controller import (
    _broken_clusterrole_rules,
    _correct_clusterrole_rules,
)


def _deployment(*, replicas=1, ready=1, generation=2, observed_generation=2):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="cleanup-controller", generation=generation),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            updated_replicas=ready,
            ready_replicas=ready,
            available_replicas=ready,
            unavailable_replicas=0 if ready == replicas else 1,
        ),
    )


def _app_pod(*, phase="Running", ready=True):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="frontend-abc", deletion_timestamp=None),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[SimpleNamespace(name="frontend", ready=ready)],
        ),
    )


class _CoreV1:
    def __init__(self, *, original_deleted=True, controller_reconciles=True):
        self.original_deleted = original_deleted
        self.controller_reconciles = controller_reconciles
        self.request_name = None
        self.request_exists = False
        self.request_deletion_requested = False
        self.created = []
        self.deleted = []
        self.patched = []

    def read_namespaced_config_map(self, name, namespace, _request_timeout):
        if name == "reservation-cleanup-token":
            if self.original_deleted:
                raise ApiException(status=404)
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    deletion_timestamp="now",
                    finalizers=["cleanup.reservations.io/pending-cleanup"],
                )
            )

        if not self.request_exists or (self.request_deletion_requested and self.controller_reconciles):
            raise ApiException(status=404)
        return SimpleNamespace(
            metadata=SimpleNamespace(
                deletion_timestamp="now" if self.request_deletion_requested else None,
                finalizers=["cleanup.reservations.io/pending-cleanup"],
            )
        )

    def create_namespaced_config_map(self, namespace, body, _request_timeout):
        self.request_name = body.metadata.name
        self.request_exists = True
        self.created.append((namespace, body))
        return body

    def delete_namespaced_config_map(self, name, namespace, body, _request_timeout):
        self.deleted.append((name, namespace, body.grace_period_seconds))
        if name == self.request_name:
            self.request_deletion_requested = True
            if self.controller_reconciles:
                self.request_exists = False

    def patch_namespaced_config_map(self, name, namespace, body, _request_timeout):
        if name == self.request_name and not self.request_exists:
            raise ApiException(status=404)
        self.patched.append((name, namespace, body))
        if name == self.request_name:
            self.request_exists = False


class _AppsV1:
    def __init__(self, deployment=None, read_error=None):
        self.deployment = deployment or _deployment()
        self.read_error = read_error

    def read_namespaced_deployment(self, name, namespace, _request_timeout):
        if self.read_error is not None:
            raise self.read_error
        return self.deployment


class _KubeCtl:
    def __init__(self, *, core_v1=None, apps_v1=None, pods=None):
        self.core_v1_api = core_v1 or _CoreV1()
        self.apps_v1_api = apps_v1 or _AppsV1()
        self.pods = [_app_pod()] if pods is None else pods

    def list_deployments(self, namespace):
        return SimpleNamespace(items=[self.apps_v1_api.deployment])

    def list_pods(self, namespace):
        controller = SimpleNamespace(
            metadata=SimpleNamespace(
                name="cleanup-controller-abc",
                deletion_timestamp=None,
            ),
            status=SimpleNamespace(phase="Running", container_statuses=[]),
        )
        return SimpleNamespace(items=[*self.pods, controller])


def _oracle(kubectl):
    problem = SimpleNamespace(namespace="hotel-reservation", kubectl=kubectl)
    oracle = FinalizerDeadlockControllerMitigationOracle(
        problem=problem,
        configmap_name="reservation-cleanup-token",
        finalizer="cleanup.reservations.io/pending-cleanup",
        controller_deployment_name="cleanup-controller",
    )
    oracle.rollout_settle_seconds = 0
    oracle.cleanup_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_accepts_real_controller_cleanup_without_clusterrole_or_annotation_checks():
    core_v1 = _CoreV1(controller_reconciles=True)
    kubectl = _KubeCtl(core_v1=core_v1)

    assert _oracle(kubectl).evaluate()["success"] is True

    request = core_v1.created[0][1]
    assert request.metadata.name.startswith("reservation-cleanup-request-")
    assert request.metadata.labels == {
        "app.kubernetes.io/component": "reservation-cleanup",
        "app.kubernetes.io/managed-by": "cleanup-controller",
    }
    assert core_v1.patched == []


def test_rejects_manual_cleanup_when_controller_cannot_handle_next_request():
    core_v1 = _CoreV1(original_deleted=True, controller_reconciles=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert len(core_v1.created) == 1
    assert len(core_v1.patched) == 1
    assert core_v1.request_exists is False


def test_rejects_original_configmap_still_stuck_without_creating_request():
    core_v1 = _CoreV1(original_deleted=False)

    assert _oracle(_KubeCtl(core_v1=core_v1)).evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_controller_scaled_to_zero_without_creating_request():
    core_v1 = _CoreV1()
    apps_v1 = _AppsV1(deployment=_deployment(replicas=0, ready=0))

    assert _oracle(_KubeCtl(core_v1=core_v1, apps_v1=apps_v1)).evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_deleted_controller_without_creating_request():
    core_v1 = _CoreV1()
    apps_v1 = _AppsV1(read_error=ApiException(status=404))

    assert _oracle(_KubeCtl(core_v1=core_v1, apps_v1=apps_v1)).evaluate()["success"] is False
    assert core_v1.created == []


def test_rejects_unhealthy_application_without_creating_request():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(core_v1=core_v1, pods=[_app_pod(phase="Pending", ready=False)])

    assert _oracle(kubectl).evaluate()["success"] is False
    assert core_v1.created == []


def test_correct_role_grants_only_the_api_operation_used_by_controller():
    correct = _correct_clusterrole_rules()[0]
    broken = _broken_clusterrole_rules()[0]

    assert correct.resources == ["configmaps"]
    assert set(correct.verbs) == {"get", "list", "watch", "patch"}
    assert "patch" not in broken.verbs
