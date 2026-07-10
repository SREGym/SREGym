"""Controller-owned finalizer deadlock caused by broken RBAC."""

import textwrap
import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.finalizer_deadlock_controller_mitigation import (
    FinalizerDeadlockControllerMitigationOracle,
)
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

_CONTROLLER_NAME = "cleanup-controller"
_CONTROLLER_SCRIPT_CONFIGMAP = "cleanup-controller-script"
_SA_NAME = "cleanup-controller"
_CLUSTERROLE_NAME = "configmap-cleanup-controller"
_CLUSTERROLEBINDING_NAME = "configmap-cleanup-controller"
_CONFIGMAP_NAME = "reservation-cleanup-token"
_FINALIZER = "cleanup.reservations.io/pending-cleanup"
_CONTROLLER_IMAGE = "python:3.12-alpine"
_MANAGED_LABELS = {"app.kubernetes.io/managed-by": _CONTROLLER_NAME}


def _correct_clusterrole_rules():
    return [
        client.V1PolicyRule(
            api_groups=[""],
            resources=["configmaps"],
            verbs=["get", "list", "watch", "patch"],
        ),
    ]


def _broken_clusterrole_rules():
    return [
        client.V1PolicyRule(
            api_groups=[""],
            resources=["configmaps"],
            verbs=["get", "list", "watch"],
        ),
    ]


class FinalizerDeadlockController(Problem):
    """A stuck finalizer that must be fixed by restoring controller RBAC."""

    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.rbac_v1 = client.RbacAuthorizationV1Api()
        self.configmap_name = _CONFIGMAP_NAME
        self.finalizer = _FINALIZER
        self.controller_name = _CONTROLLER_NAME
        self.sa_name = _SA_NAME
        self.clusterrole_name = _CLUSTERROLE_NAME
        self.clusterrolebinding_name = _CLUSTERROLEBINDING_NAME
        self.faulty_service = self.configmap_name
        self.root_cause = self.build_structured_root_cause(
            component=f"ClusterRole/{self.clusterrole_name} and configmap/{self.configmap_name}",
            namespace=self.namespace,
            description=(
                f"ConfigMap `{self.configmap_name}` is stuck in Terminating with finalizer `{self.finalizer}`. "
                f"The finalizer is owned by Deployment `{self.controller_name}` using ServiceAccount "
                f"`{self.sa_name}`, but ClusterRole `{self.clusterrole_name}` was changed to read-only and is "
                "missing permission to patch ConfigMaps. The controller pod repeatedly logs HTTP 403 Forbidden "
                "while trying to remove the finalizer. Restore effective RBAC for the controller ServiceAccount "
                "so its normal reconcile loop can remove finalizers and Kubernetes can finish deleting both the "
                "current ConfigMap and future cleanup requests."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = FinalizerDeadlockControllerMitigationOracle(
            problem=self,
            configmap_name=self.configmap_name,
            finalizer=self.finalizer,
            controller_deployment_name=self.controller_name,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        self._delete_support_resources()
        self._create_service_account()
        self._create_clusterrole(_correct_clusterrole_rules())
        self._create_clusterrolebinding()
        self._create_controller_script()
        self._create_finalized_configmap()
        self._deploy_controller()
        self._wait_for_controller_ready()
        print(f"Controller `{self.controller_name}` started with valid RBAC")

        self._replace_clusterrole(_broken_clusterrole_rules())
        print(f"ClusterRole `{self.clusterrole_name}` changed to read-only")

        self.kubectl.core_v1_api.delete_namespaced_config_map(
            self.configmap_name,
            self.namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0),
            _request_timeout=10,
        )
        print(f"Deleted ConfigMap `{self.configmap_name}` with --wait=false semantics")
        self._wait_for_forbidden_log()

        print(
            f"Resource: configmap/{self.configmap_name} | Controller: deployment/{self.controller_name} "
            f"| Namespace: {self.namespace}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        self._restore_clusterrole()
        if not self._wait_until_configmap_deleted(timeout_seconds=60):
            raise TimeoutError(f"Controller did not delete ConfigMap `{self.configmap_name}` after RBAC recovery")
        print(f"ClusterRole `{self.clusterrole_name}` restored; controller completed cleanup")

        print(f"Resource: configmap/{self.configmap_name} | Namespace: {self.namespace}\n")

    def _create_service_account(self):
        body = client.V1ServiceAccount(
            metadata=client.V1ObjectMeta(
                name=self.sa_name,
                namespace=self.namespace,
                labels=dict(_MANAGED_LABELS),
            )
        )
        try:
            self.kubectl.core_v1_api.create_namespaced_service_account(
                namespace=self.namespace,
                body=body,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_clusterrole(self, rules):
        body = client.V1ClusterRole(
            metadata=client.V1ObjectMeta(
                name=self.clusterrole_name,
                labels=dict(_MANAGED_LABELS),
            ),
            rules=rules,
        )
        try:
            self.rbac_v1.create_cluster_role(body=body, _request_timeout=10)
        except ApiException as e:
            if e.status == 409:
                self._replace_clusterrole(rules)
            else:
                raise

    def _replace_clusterrole(self, rules):
        body = client.V1ClusterRole(
            metadata=client.V1ObjectMeta(
                name=self.clusterrole_name,
                labels=dict(_MANAGED_LABELS),
            ),
            rules=rules,
        )
        self.rbac_v1.replace_cluster_role(
            name=self.clusterrole_name,
            body=body,
            _request_timeout=10,
        )

    def _restore_clusterrole(self):
        try:
            self._replace_clusterrole(_correct_clusterrole_rules())
        except ApiException as e:
            if e.status != 404:
                raise
            self._create_clusterrole(_correct_clusterrole_rules())

    def _create_clusterrolebinding(self):
        body = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {
                "name": self.clusterrolebinding_name,
                "labels": dict(_MANAGED_LABELS),
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": self.clusterrole_name,
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": self.sa_name,
                    "namespace": self.namespace,
                }
            ],
        }
        try:
            self.rbac_v1.create_cluster_role_binding(body=body, _request_timeout=10)
        except ApiException as e:
            if e.status == 409:
                self.rbac_v1.replace_cluster_role_binding(
                    name=self.clusterrolebinding_name,
                    body=body,
                    _request_timeout=10,
                )
            else:
                raise

    def _create_controller_script(self):
        script = textwrap.dedent(
            """\
            import json
            import os
            import ssl
            import time
            import urllib.error
            import urllib.parse
            import urllib.request
            from pathlib import Path

            NAMESPACE = "__NAMESPACE__"
            FINALIZER = "__FINALIZER__"
            LABEL_SELECTOR = "app.kubernetes.io/component=reservation-cleanup"
            TOKEN_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
            CA_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
            API_SERVER = (
                f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:"
                f"{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}"
            )
            TLS_CONTEXT = ssl.create_default_context(cafile=CA_FILE)


            def api_request(method, path, payload=None):
                data = None if payload is None else json.dumps(payload).encode()
                headers = {
                    "Authorization": f"Bearer {TOKEN_FILE.read_text().strip()}",
                    "Accept": "application/json",
                }
                if payload is not None:
                    headers["Content-Type"] = "application/merge-patch+json"
                request = urllib.request.Request(
                    f"{API_SERVER}{path}",
                    data=data,
                    headers=headers,
                    method=method,
                )
                with urllib.request.urlopen(request, context=TLS_CONTEXT, timeout=10) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}


            def reconcile():
                selector = urllib.parse.quote(LABEL_SELECTOR, safe="")
                response = api_request(
                    "GET",
                    f"/api/v1/namespaces/{NAMESPACE}/configmaps?labelSelector={selector}",
                )
                for configmap in response.get("items", []):
                    metadata = configmap.get("metadata", {})
                    finalizers = metadata.get("finalizers", [])
                    if not metadata.get("deletionTimestamp") or FINALIZER not in finalizers:
                        continue

                    name = metadata["name"]
                    remaining = [item for item in finalizers if item != FINALIZER]
                    print(
                        f"ConfigMap {name} is Terminating; attempting controller-owned cleanup",
                        flush=True,
                    )
                    encoded_name = urllib.parse.quote(name, safe="")
                    api_request(
                        "PATCH",
                        f"/api/v1/namespaces/{NAMESPACE}/configmaps/{encoded_name}",
                        {"metadata": {"finalizers": remaining}},
                    )
                    print(f"ConfigMap {name} cleanup completed", flush=True)


            print(f"cleanup-controller starting namespace={NAMESPACE}", flush=True)
            while True:
                try:
                    reconcile()
                except urllib.error.HTTPError as error:
                    if error.code == 403:
                        print(
                            "cleanup-controller reconciliation failed: "
                            "Kubernetes API denied cleanup request with HTTP 403 Forbidden",
                            flush=True,
                        )
                    else:
                        print(
                            f"cleanup-controller reconciliation failed: Kubernetes API returned HTTP {error.code}",
                            flush=True,
                        )
                except Exception as error:
                    print(
                        f"cleanup-controller reconciliation failed: {type(error).__name__}",
                        flush=True,
                    )
                time.sleep(2)
            """
        )
        script = script.replace("__NAMESPACE__", self.namespace).replace("__FINALIZER__", self.finalizer)
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=_CONTROLLER_SCRIPT_CONFIGMAP,
                namespace=self.namespace,
                labels=dict(_MANAGED_LABELS),
            ),
            data={"controller.py": script},
        )
        try:
            self.kubectl.core_v1_api.create_namespaced_config_map(
                namespace=self.namespace,
                body=body,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status == 409:
                self.kubectl.core_v1_api.replace_namespaced_config_map(
                    name=_CONTROLLER_SCRIPT_CONFIGMAP,
                    namespace=self.namespace,
                    body=body,
                    _request_timeout=10,
                )
            else:
                raise

    def _create_finalized_configmap(self):
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=self.configmap_name,
                namespace=self.namespace,
                finalizers=[self.finalizer],
                labels={"app.kubernetes.io/component": "reservation-cleanup"},
            ),
            data={
                "cleanup-token": "pending",
                "source": "reservation-maintenance",
            },
        )
        try:
            self.kubectl.core_v1_api.create_namespaced_config_map(
                namespace=self.namespace,
                body=body,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status != 409:
                raise
            self._force_clear_finalizer()
            self._wait_until_configmap_deleted(timeout_seconds=30)
            self.kubectl.core_v1_api.create_namespaced_config_map(
                namespace=self.namespace,
                body=body,
                _request_timeout=10,
            )

    def _deploy_controller(self):
        body = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=self.controller_name,
                namespace=self.namespace,
                labels={"app": self.controller_name, **_MANAGED_LABELS},
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": self.controller_name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": self.controller_name}),
                    spec=client.V1PodSpec(
                        service_account_name=self.sa_name,
                        automount_service_account_token=True,
                        containers=[
                            client.V1Container(
                                name="controller",
                                image=_CONTROLLER_IMAGE,
                                image_pull_policy="IfNotPresent",
                                command=["python", "/controller/controller.py"],
                                volume_mounts=[
                                    client.V1VolumeMount(
                                        name="controller-script",
                                        mount_path="/controller",
                                        read_only=True,
                                    )
                                ],
                            )
                        ],
                        volumes=[
                            client.V1Volume(
                                name="controller-script",
                                config_map=client.V1ConfigMapVolumeSource(
                                    name=_CONTROLLER_SCRIPT_CONFIGMAP,
                                    default_mode=0o555,
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )
        try:
            self.kubectl.apps_v1_api.create_namespaced_deployment(
                namespace=self.namespace,
                body=body,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status == 409:
                self.kubectl.apps_v1_api.replace_namespaced_deployment(
                    name=self.controller_name,
                    namespace=self.namespace,
                    body=body,
                    _request_timeout=10,
                )
            else:
                raise

    def _wait_for_controller_ready(self, timeout_seconds: int = 180):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            dep = self.kubectl.apps_v1_api.read_namespaced_deployment(
                self.controller_name,
                self.namespace,
                _request_timeout=10,
            )
            if (dep.status.ready_replicas or 0) >= 1:
                return
            time.sleep(2)
        raise TimeoutError(f"Timed out waiting for Deployment `{self.controller_name}` to become ready")

    def _wait_for_forbidden_log(self, timeout_seconds: int = 45):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            pods = self.kubectl.core_v1_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"app={self.controller_name}",
                _request_timeout=10,
            ).items
            for pod in pods:
                try:
                    logs = self.kubectl.core_v1_api.read_namespaced_pod_log(
                        name=pod.metadata.name,
                        namespace=self.namespace,
                        tail_lines=20,
                        _request_timeout=10,
                    )
                except ApiException:
                    continue
                if "Kubernetes API denied cleanup request with HTTP 403 Forbidden" in logs:
                    return
            time.sleep(2)
        print("Controller did not report an authorization failure before timeout; deletion remained blocked")

    def _remove_configmap_finalizers(self):
        try:
            cm = self.kubectl.core_v1_api.read_namespaced_config_map(
                self.configmap_name,
                self.namespace,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise

        if not (cm.metadata.finalizers or []):
            return

        output = self.kubectl.exec_command(
            f"kubectl patch configmap {self.configmap_name} -n {self.namespace} "
            '--type=json -p \'[{"op":"remove","path":"/metadata/finalizers"}]\' --request-timeout=10s'
        )
        output_lower = output.lower()
        if "not found" in output_lower:
            return
        if "patched" not in output_lower and "no change" not in output_lower:
            raise RuntimeError(f"Failed to remove finalizers from ConfigMap {self.configmap_name}: {output.strip()}")

    def _wait_until_configmap_deleted(self, timeout_seconds: int):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self.kubectl.core_v1_api.read_namespaced_config_map(
                    self.configmap_name,
                    self.namespace,
                    _request_timeout=10,
                )
            except ApiException as e:
                if e.status == 404:
                    return True
                raise
            time.sleep(2)
        return False

    def _force_clear_finalizer(self):
        try:
            self._remove_configmap_finalizers()
        except ApiException as e:
            if e.status != 404:
                raise

    def _delete_support_resources(self):
        with _ignore_not_found():
            self.kubectl.apps_v1_api.delete_namespaced_deployment(
                name=self.controller_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
                _request_timeout=10,
            )
        with _ignore_not_found():
            self.kubectl.core_v1_api.delete_namespaced_config_map(
                name=_CONTROLLER_SCRIPT_CONFIGMAP,
                namespace=self.namespace,
                _request_timeout=10,
            )
        with _ignore_not_found():
            self.kubectl.core_v1_api.delete_namespaced_config_map(
                name=self.configmap_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
                _request_timeout=10,
            )
        with _ignore_not_found():
            self._force_clear_finalizer()
        self._wait_until_configmap_deleted(timeout_seconds=30)
        with _ignore_not_found():
            self.rbac_v1.delete_cluster_role_binding(
                name=self.clusterrolebinding_name,
                _request_timeout=10,
            )
        with _ignore_not_found():
            self.rbac_v1.delete_cluster_role(
                name=self.clusterrole_name,
                _request_timeout=10,
            )
        with _ignore_not_found():
            self.kubectl.core_v1_api.delete_namespaced_service_account(
                name=self.sa_name,
                namespace=self.namespace,
                _request_timeout=10,
            )


class _ignore_not_found:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return isinstance(exc, ApiException) and exc.status == 404
