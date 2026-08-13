import json
from types import SimpleNamespace

from kubernetes.client.rest import ApiException

from sregym.conductor.problems.admission_webhook_outage import AdmissionWebhookOutage


class _AdmissionApi:
    def __init__(self, current=None):
        self.current = current
        self.created = []
        self.replaced = []
        self.deleted = []

    def read_validating_webhook_configuration(self, name):
        if self.current is None:
            raise ApiException(status=404)
        return self.current

    def create_validating_webhook_configuration(self, body):
        self.created.append(body)

    def replace_validating_webhook_configuration(self, name, body):
        self.replaced.append((name, body))

    def delete_validating_webhook_configuration(self, name):
        self.deleted.append(name)
        self.current = None


class _KubeCtl:
    def __init__(self):
        self.commands = []
        self.deployment_reads = []
        self.wait_calls = []

    def exec_command(self, command):
        self.commands.append(command)
        return "ok"

    def get_deployment(self, name, namespace):
        self.deployment_reads.append((name, namespace))
        return SimpleNamespace(metadata=SimpleNamespace(name=name))

    def wait_for_ready(self, namespace, service_names, max_wait):
        self.wait_calls.append((namespace, service_names, max_wait))


def _problem(tmp_path, admission_api, kubectl=None):
    problem = AdmissionWebhookOutage.__new__(AdmissionWebhookOutage)
    problem.namespace = "hotel-reservation"
    problem.faulty_service = "recommendation"
    problem.admission_api = admission_api
    problem.kubectl = kubectl or _KubeCtl()
    problem.fault_injected = True
    problem._webhook_state_path = lambda: tmp_path / "webhook-state.json"
    return problem


def _baseline_configuration():
    return {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingWebhookConfiguration",
        "metadata": {
            "name": AdmissionWebhookOutage.WEBHOOK_NAME,
            "uid": "baseline-uid",
            "resourceVersion": "7",
            "creationTimestamp": "yesterday",
            "labels": {"managed-by": "platform"},
        },
        "webhooks": [
            {
                "name": "existing.policy.example.com",
                "failurePolicy": "Ignore",
            }
        ],
    }


def test_capture_records_that_no_same_named_webhook_existed(tmp_path):
    problem = _problem(tmp_path, _AdmissionApi())

    path = problem._capture_webhook_baseline()

    assert json.loads(path.read_text()) == {"existed": False}


def test_capture_preserves_a_preexisting_same_named_webhook(tmp_path):
    baseline = _baseline_configuration()
    problem = _problem(tmp_path, _AdmissionApi(current=baseline))

    path = problem._capture_webhook_baseline()

    assert json.loads(path.read_text()) == {
        "existed": True,
        "configuration": baseline,
    }


def test_restore_replaces_injected_webhook_with_saved_configuration(tmp_path):
    current = SimpleNamespace(metadata=SimpleNamespace(resource_version="99"))
    admission_api = _AdmissionApi(current=current)
    problem = _problem(tmp_path, admission_api)
    path = problem._webhook_state_path()
    path.write_text(
        json.dumps(
            {
                "existed": True,
                "configuration": _baseline_configuration(),
            }
        )
    )

    problem._restore_webhook_baseline()

    assert len(admission_api.replaced) == 1
    name, restored = admission_api.replaced[0]
    assert name == AdmissionWebhookOutage.WEBHOOK_NAME
    assert restored["metadata"] == {
        "name": AdmissionWebhookOutage.WEBHOOK_NAME,
        "resourceVersion": "99",
        "labels": {"managed-by": "platform"},
    }
    assert restored["webhooks"] == _baseline_configuration()["webhooks"]
    assert path.exists()


def test_recovery_deletes_only_an_injected_webhook_and_waits_for_target(tmp_path):
    admission_api = _AdmissionApi(current=SimpleNamespace(metadata=SimpleNamespace(resource_version="99")))
    kubectl = _KubeCtl()
    problem = _problem(tmp_path, admission_api, kubectl)
    path = problem._webhook_state_path()
    path.write_text(json.dumps({"existed": False}))

    problem.recover_fault()

    assert admission_api.deleted == [AdmissionWebhookOutage.WEBHOOK_NAME]
    assert kubectl.commands == ["kubectl rollout status deployment/recommendation -n hotel-reservation --timeout=120s"]
    assert kubectl.deployment_reads == [("recommendation", "hotel-reservation")]
    assert kubectl.wait_calls == [("hotel-reservation", "recommendation", 180)]
    assert not path.exists()
    assert problem.fault_injected is False


def test_recovery_requires_saved_ownership_state_before_deleting_webhook(tmp_path):
    admission_api = _AdmissionApi(current=SimpleNamespace(metadata=SimpleNamespace(resource_version="99")))
    problem = _problem(tmp_path, admission_api)

    problem.recover_fault()

    assert admission_api.deleted == []
    assert problem.fault_injected is False
