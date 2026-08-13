import json
from types import SimpleNamespace

from sregym.conductor.problems.secret_rotation_stale_env_credentials import (
    SecretRotationStaleEnvCredentialsAstronomyShop,
)


class _AppsV1:
    def __init__(self, deployment):
        self.deployment = deployment
        self.patches = []

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append((name, namespace, body))
        annotations = self.deployment.metadata.annotations
        for key, value in body["metadata"]["annotations"].items():
            if value is None:
                annotations.pop(key, None)
            else:
                annotations[key] = value


class _KubeCtl:
    def __init__(self, stale_uid=None):
        annotations = {}
        if stale_uid is not None:
            annotations[SecretRotationStaleEnvCredentialsAstronomyShop.SOURCE_POD_UID_ANNOTATION] = stale_uid
        self.deployment = SimpleNamespace(metadata=SimpleNamespace(annotations=annotations))
        self.apps_v1_api = _AppsV1(self.deployment)

    def get_deployment(self, name, namespace):
        return self.deployment


def _problem(kubectl):
    problem = SecretRotationStaleEnvCredentialsAstronomyShop.__new__(SecretRotationStaleEnvCredentialsAstronomyShop)
    problem.kubectl = kubectl
    problem.namespace = "astronomy-shop"
    problem.faulty_service = "product-catalog"
    problem.secret_name = "product-catalog-db-conn"
    problem.secret_key = "DB_CONNECTION_STRING"
    problem.old_conn = "postgres://otelu:otelp@postgresql/otel?sslmode=disable"
    problem.new_conn = "postgres://otelu:otelp_7k9m2q4x@postgresql/otel?sslmode=disable"
    problem.stale_product_catalog_pod_uid = None
    return problem


def _deployment_json():
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "product-catalog",
                            "env": [
                                {
                                    "name": "DB_CONNECTION_STRING",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "product-catalog-db-conn",
                                            "key": "DB_CONNECTION_STRING",
                                        }
                                    },
                                }
                            ],
                        }
                    ]
                }
            }
        }
    }


def test_persists_and_clears_stale_pod_uid_on_deployment_metadata():
    kubectl = _KubeCtl()
    problem = _problem(kubectl)

    problem._set_stale_product_catalog_pod_uid("stale-uid")

    assert problem._get_stale_product_catalog_pod_uid() == "stale-uid"
    assert problem.stale_product_catalog_pod_uid == "stale-uid"
    assert kubectl.apps_v1_api.patches[-1][2] == {
        "metadata": {
            "annotations": {
                "credential-source-pod-uid": "stale-uid",
            }
        }
    }

    problem._clear_stale_product_catalog_pod_uid()

    assert problem._get_stale_product_catalog_pod_uid() is None
    assert problem.stale_product_catalog_pod_uid is None


def test_fresh_problem_instance_derives_stale_runtime_from_cluster_marker():
    problem = _problem(_KubeCtl(stale_uid="stale-uid"))
    problem._get_product_catalog_pod = lambda: SimpleNamespace(metadata=SimpleNamespace(uid="stale-uid"))
    problem._get_secret_conn_string = lambda: problem.new_conn
    problem._run = lambda command: json.dumps(_deployment_json())

    assert problem.stale_product_catalog_pod_uid is None
    assert problem._get_product_catalog_env() == problem.old_conn


def test_replacement_pod_is_inferred_to_use_current_secret_value():
    problem = _problem(_KubeCtl(stale_uid="stale-uid"))
    problem._get_product_catalog_pod = lambda: SimpleNamespace(metadata=SimpleNamespace(uid="replacement-uid"))
    problem._get_secret_conn_string = lambda: problem.new_conn
    problem._run = lambda command: json.dumps(_deployment_json())

    assert problem._get_product_catalog_env() == problem.new_conn


def test_ambiguous_rotation_response_is_accepted_after_password_verification():
    problem = _problem(_KubeCtl())
    problem.db_user = "otelu"
    problem._POSTGRES_ROTATION_ATTEMPTS = 3
    problem._POSTGRES_PASSWORD_CHECK_INTERVAL_SECONDS = 0
    calls = []
    problem._postgres_exec = lambda password, sql: calls.append((password, sql)) or "connection timed out"
    problem._postgres_accepts_password = lambda password: password == "new-password"

    problem._rotate_postgres_password("old-password", "new-password")

    assert len(calls) == 1
