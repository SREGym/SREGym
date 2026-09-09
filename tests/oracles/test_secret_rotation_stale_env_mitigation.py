import json
import shlex
import subprocess
from types import MethodType, SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.secret_rotation_stale_env_mitigation import (
    SecretRotationStaleEnvMitigation,
)
from sregym.service.kubectl import KubeCtl

OLD_CONN = "postgres://otelu:otelp@postgresql/otel?sslmode=disable"
NEW_CONN = "postgres://otelu:otelp_7k9m2q4x@postgresql/otel?sslmode=disable"
NOVEL_CONN = "postgres://otelu:different-password@postgresql/otel?sslmode=disable"
MARKER = "credential-source-pod-uid"


def _deployment_json(*, stale_uid="stale-uid", conn_source="secret"):
    annotations = {} if stale_uid is None else {MARKER: stale_uid}
    env = {"name": "DB_CONNECTION_STRING"}
    if conn_source == "secret":
        env["valueFrom"] = {
            "secretKeyRef": {
                "name": "product-catalog-db-conn",
                "key": "DB_CONNECTION_STRING",
            }
        }
    else:
        env["value"] = conn_source
    return {
        "metadata": {"annotations": annotations},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "product-catalog",
                            "env": [env],
                        }
                    ]
                }
            }
        },
    }


def _deployment(
    *,
    replicas=1,
    generation=1,
    observed_generation=1,
    current_replicas=1,
    updated=1,
    ready=1,
    available=1,
    unavailable=0,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="product-catalog", generation=generation),
        spec=SimpleNamespace(
            replicas=replicas,
            selector=SimpleNamespace(match_labels={"opentelemetry.io/name": "product-catalog"}),
        ),
        status=SimpleNamespace(
            observed_generation=observed_generation,
            replicas=current_replicas,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=unavailable,
        ),
    )


def _pod(name="product-catalog-abc", uid="replacement-uid", app="product-catalog"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=uid,
            deletion_timestamp=None,
            labels={"opentelemetry.io/name": app},
        )
    )


def _endpoint(pod_name):
    return SimpleNamespace(target_ref=SimpleNamespace(kind="Pod", name=pod_name))


class _CoreV1:
    def __init__(self, *, endpoint_pods=None, probe_phase="Succeeded", probe_logs="PRODUCTS_OK\n"):
        endpoint_pods = ["product-catalog-abc"] if endpoint_pods is None else endpoint_pods
        self.endpoints = SimpleNamespace(
            subsets=[SimpleNamespace(addresses=[_endpoint(name) for name in endpoint_pods])]
        )
        self.probe_phase = probe_phase
        self.probe_logs = probe_logs
        self.created_pods = []
        self.deleted_pods = []

    def read_namespaced_endpoints(self, name, namespace):
        return self.endpoints

    def read_namespaced_service(self, name, namespace):
        return SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(port=8080)]))

    def create_namespaced_pod(self, namespace, body):
        self.created_pods.append((namespace, body))

    def read_namespaced_pod(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(phase=self.probe_phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.probe_logs

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds):
        self.deleted_pods.append((name, namespace, grace_period_seconds))


class _KubeCtl:
    def __init__(self, *, deployment_json=None, deployment=None, pods=None, core_v1=None):
        self.deployment_json = deployment_json or _deployment_json()
        self.deployment = deployment or _deployment()
        self.pods = [_pod()] if pods is None else pods
        self.core_v1_api = core_v1 or _CoreV1()

    def exec_command(self, command):
        if "kubectl get deployment" in command:
            return json.dumps(self.deployment_json)
        raise AssertionError(f"Unexpected command: {command}")

    def exec_command_checked(self, command, timeout=None):
        return self.exec_command(command)

    def get_deployment(self, name, namespace):
        return self.deployment

    def list_pods(self, namespace):
        return SimpleNamespace(items=self.pods)


def _oracle(
    kubectl,
    *,
    secret_conn=NEW_CONN,
    accepted_passwords=None,
    init_uses_new=True,
):
    problem = SimpleNamespace(
        namespace="astronomy-shop",
        faulty_service="product-catalog",
        backend_service="postgresql",
        secret_name="product-catalog-db-conn",
        secret_key="DB_CONNECTION_STRING",
        db_user="otelu",
        db_name="otel",
        old_password="otelp",
        new_password="otelp_7k9m2q4x",
        old_conn=OLD_CONN,
        new_conn=NEW_CONN,
        SOURCE_POD_UID_ANNOTATION=MARKER,
        _POSTGRES_PASSWORD_CHECK_ATTEMPTS=1,
        _POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS=0,
        kubectl=kubectl,
        _get_secret_conn_string=lambda: secret_conn,
        _postgresql_init_uses_password=lambda password: init_uses_new,
    )
    oracle = SecretRotationStaleEnvMitigation(problem)
    accepted_passwords = {problem.new_password} if accepted_passwords is None else accepted_passwords
    oracle._postgres_accepts_password = lambda password: password in accepted_passwords
    oracle.rollout_timeout_seconds = 0
    oracle.poll_interval_seconds = 0
    return oracle


def test_fresh_oracle_rejects_current_pod_matching_cluster_stale_uid():
    core_v1 = _CoreV1()
    kubectl = _KubeCtl(pods=[_pod(uid="stale-uid")], core_v1=core_v1)

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert result["reason"] == "stale_pod_still_serving"
    # The prose that used to *be* the reason is preserved verbatim.
    assert "before credential rotation" in result["detail"]["message"]
    assert core_v1.created_pods == []


def test_accepts_replacement_pod_using_required_new_password_and_product_data():
    core_v1 = _CoreV1()
    result = _oracle(_KubeCtl(core_v1=core_v1)).evaluate()

    assert result["success"] is True
    assert result["product_probe_succeeded"] is True
    assert len(core_v1.created_pods) == 1
    script = core_v1.created_pods[0][1].spec.containers[0].command[-1]
    assert script.startswith("set -eu;")
    assert "/api/products" in script
    assert "OLJCESPC7Z" in script
    assert "-T 5 -t 1" in script
    assert len(core_v1.deleted_pods) == 1


def test_accepts_literal_required_new_connection_after_replacement():
    deployment_json = _deployment_json(conn_source=NEW_CONN)

    assert _oracle(_KubeCtl(deployment_json=deployment_json)).evaluate()["success"] is True


def test_deleting_only_stale_marker_still_fails_functional_probe():
    core_v1 = _CoreV1(probe_phase="Failed", probe_logs="")
    deployment_json = _deployment_json(stale_uid=None)
    kubectl = _KubeCtl(deployment_json=deployment_json, pods=[_pod(uid="stale-uid")], core_v1=core_v1)

    result = _oracle(kubectl).evaluate()

    assert result["success"] is False
    assert result["reason"] == "product_probe_failed"
    # The prose that used to *be* the reason is preserved verbatim.
    assert "/api/products" in result["detail"]["message"]
    assert len(core_v1.deleted_pods) == 1


@pytest.mark.parametrize("conn", [OLD_CONN, NOVEL_CONN])
def test_rejects_rollback_or_novel_secret_password(conn):
    core_v1 = _CoreV1()
    result = _oracle(_KubeCtl(core_v1=core_v1), secret_conn=conn).evaluate()

    assert result["success"] is False
    assert result["reason"] == "secret_not_rotated"
    # The prose that used to *be* the reason is preserved verbatim.
    assert "Secret does not contain" in result["detail"]["message"]
    assert core_v1.created_pods == []


def test_rejects_backend_that_still_accepts_old_password():
    result = _oracle(
        _KubeCtl(),
        accepted_passwords={"otelp", "otelp_7k9m2q4x"},
    ).evaluate()

    assert result["success"] is False
    assert result["reason"] == "postgres_still_accepts_old_password"
    # The prose that used to *be* the reason is preserved verbatim.
    assert "pre-rotation password" in result["detail"]["message"]


def test_rejects_scaled_to_zero_without_starting_probe():
    core_v1 = _CoreV1()
    deployment = _deployment(
        replicas=0,
        current_replicas=0,
        updated=0,
        ready=0,
        available=0,
    )

    result = _oracle(_KubeCtl(deployment=deployment, pods=[], core_v1=core_v1)).evaluate()

    assert result["success"] is False
    assert result["reason"] == "required_deployment_scaled_to_zero"
    # The prose that used to *be* the reason is preserved verbatim.
    assert "scaled to 0" in result["detail"]["message"]
    assert core_v1.created_pods == []


def test_rejects_stale_rollout_even_when_old_pod_is_ready():
    core_v1 = _CoreV1()
    deployment = _deployment(
        generation=2,
        observed_generation=1,
        updated=0,
        ready=1,
        available=1,
        unavailable=1,
    )

    result = _oracle(_KubeCtl(deployment=deployment, core_v1=core_v1)).evaluate()

    assert result["success"] is False
    assert result["reason"] == "required_deployment_not_rolled_out"
    # The prose that used to *be* the reason is preserved verbatim.
    assert "current rollout" in result["detail"]["message"]
    assert core_v1.created_pods == []


def test_rejects_endpoint_from_another_workload():
    core_v1 = _CoreV1(endpoint_pods=["frontend-abc"])
    pods = [_pod(), _pod(name="frontend-abc", uid="frontend-uid", app="frontend")]

    result = _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()

    assert result["success"] is False
    assert result["reason"] == "no_ready_endpoints"
    # The prose that used to *be* the reason is preserved verbatim.
    assert "no ready endpoint" in result["detail"]["message"]
    assert core_v1.created_pods == []


def _real_password_oracle():
    oracle = _oracle(_KubeCtl())
    del oracle.__dict__["_postgres_accepts_password"]
    return oracle


@pytest.mark.parametrize("stage", ["deployment", "old_password", "new_password"])
@pytest.mark.parametrize("timeout", [False, True])
def test_checked_command_errors_do_not_claim_password_rejection(monkeypatch, stage, timeout):
    oracle = _real_password_oracle()
    kube = oracle.problem.kubectl
    kube.exec_command_checked = MethodType(KubeCtl.exec_command_checked, kube)

    def run(command, **kwargs):
        current = (
            "deployment"
            if "get deployment" in command
            else ("new_password" if oracle.new_password in command else "old_password")
        )
        if current == stage:
            if timeout:
                raise subprocess.TimeoutExpired(command, 30)
            raise subprocess.CalledProcessError(1, command, stderr=b"connection refused")
        output = json.dumps(kube.deployment_json) if current == "deployment" else "PASSWORD_REJECTED\n"
        return subprocess.CompletedProcess(command, 0, stdout=output.encode())

    monkeypatch.setattr(subprocess, "run", run)
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["reason"] == "oracle_command_failed"
    assert result["failure_class"] == "ambiguous"


@pytest.mark.parametrize(
    ("output", "exit_code", "expected"),
    [
        ("1", 0, True),
        ('psql: FATAL:  password authentication failed for user "otelu"', 2, False),
        ("connection refused", 2, "command_error"),
        ('FATAL: database "otel" does not exist', 2, "command_error"),
        ("psql: not found", 127, "command_error"),
        ("unexpected response", 0, None),
        ("0", 0, None),
    ],
)
def test_password_probe_shell_preserves_non_authentication_errors(monkeypatch, output, exit_code, expected):
    oracle = _real_password_oracle()

    def run_probe(command):
        script = shlex.split(command)[-1]
        stub = f"psql() {{ printf '%s\\n' {shlex.quote(output)}; return {exit_code}; }}; "
        return subprocess.run(["sh", "-c", stub + script], check=True, capture_output=True, text=True).stdout

    monkeypatch.setattr(oracle, "_run", run_probe)
    if expected == "command_error":
        with pytest.raises(subprocess.CalledProcessError) as error:
            oracle._postgres_accepts_password(oracle.new_password)
        assert output in error.value.stderr
    else:
        assert oracle._postgres_accepts_password(oracle.new_password) is expected


@pytest.mark.parametrize("response", ["", "0", "unexpected response"])
def test_unreadable_password_response_is_ambiguous(monkeypatch, response):
    oracle = _real_password_oracle()
    original = oracle._run
    monkeypatch.setattr(oracle, "_run", lambda command: response if "kubectl exec" in command else original(command))
    result = oracle.evaluate()
    assert result["reason"] == "postgres_password_probe_unreadable"
    assert result["failure_class"] == "ambiguous"


def test_confirmed_password_rejection_can_recover_on_retry(monkeypatch):
    oracle = _real_password_oracle()
    oracle.problem._POSTGRES_PASSWORD_CHECK_ATTEMPTS = 3
    responses = iter(["PASSWORD_REJECTED", "1"])
    monkeypatch.setattr(oracle, "_run", lambda command: next(responses))
    assert oracle._postgres_accepts_password(oracle.new_password) is True


@pytest.mark.parametrize("status", [403, 404, 503])
@pytest.mark.parametrize("stage", ["deployment", "password", "product"])
def test_api_errors_preserve_their_classification(monkeypatch, status, stage):
    oracle = _oracle(_KubeCtl())

    def fail(*args, **kwargs):
        raise ApiException(status=status)

    if stage == "deployment":
        monkeypatch.setattr(oracle.problem.kubectl, "get_deployment", fail)
    elif stage == "password":
        monkeypatch.setattr(oracle, "_postgres_accepts_password", fail)
    else:
        monkeypatch.setattr(oracle.problem.kubectl.core_v1_api, "create_namespaced_pod", fail)
    result = oracle.evaluate()
    assert result["success"] is False
    assert result["failure_class"] == ("environment_error" if status == 503 else "ambiguous")
    assert result["detail"]["status"] == status
