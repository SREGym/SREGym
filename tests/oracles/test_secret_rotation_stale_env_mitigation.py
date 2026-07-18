import json
from types import SimpleNamespace

import pytest

from sregym.conductor.oracles.secret_rotation_stale_env_mitigation import (
    SecretRotationStaleEnvMitigation,
)

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
    assert "before credential rotation" in result["reason"]
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
    assert "/api/products" in result["reason"]
    assert len(core_v1.deleted_pods) == 1


@pytest.mark.parametrize("conn", [OLD_CONN, NOVEL_CONN])
def test_rejects_rollback_or_novel_secret_password(conn):
    core_v1 = _CoreV1()
    result = _oracle(_KubeCtl(core_v1=core_v1), secret_conn=conn).evaluate()

    assert result["success"] is False
    assert "Secret does not contain" in result["reason"]
    assert core_v1.created_pods == []


def test_rejects_backend_that_still_accepts_old_password():
    result = _oracle(
        _KubeCtl(),
        accepted_passwords={"otelp", "otelp_7k9m2q4x"},
    ).evaluate()

    assert result["success"] is False
    assert "pre-rotation password" in result["reason"]


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
    assert "scaled to 0" in result["reason"]
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
    assert "current rollout" in result["reason"]
    assert core_v1.created_pods == []


def test_rejects_endpoint_from_another_workload():
    core_v1 = _CoreV1(endpoint_pods=["frontend-abc"])
    pods = [_pod(), _pod(name="frontend-abc", uid="frontend-uid", app="frontend")]

    result = _oracle(_KubeCtl(pods=pods, core_v1=core_v1)).evaluate()

    assert result["success"] is False
    assert "no ready endpoint" in result["reason"]
    assert core_v1.created_pods == []
