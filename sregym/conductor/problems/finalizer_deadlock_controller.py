"""Controller-owned finalizer deadlock caused by broken RBAC."""

import textwrap
import threading
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
_CONTROLLER_IMAGE = "mongo:4.4.6"
_CONTROLLER_COMPLETION_ANNOTATION = "cleanup-controller.platform.io/finalizer-cleanup-completed"


def _correct_clusterrole_rules():
    return [
        client.V1PolicyRule(
            api_groups=[""],
            resources=["configmaps"],
            verbs=["get", "list", "watch", "patch", "update"],
        ),
        client.V1PolicyRule(
            api_groups=[""],
            resources=["configmaps/finalizers"],
            verbs=["patch", "update"],
        ),
        client.V1PolicyRule(
            api_groups=["apps"],
            resources=["deployments"],
            verbs=["get", "patch"],
        ),
    ]


def _broken_clusterrole_rules():
    return [
        client.V1PolicyRule(
            api_groups=[""],
            resources=["configmaps"],
            verbs=["get", "list", "watch"],
        ),
        client.V1PolicyRule(
            api_groups=["apps"],
            resources=["deployments"],
            verbs=["get", "patch"],
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
        self._reconcile_stop = threading.Event()
        self._reconcile_thread = None

        self.root_cause = self.build_structured_root_cause(
            component=f"ClusterRole/{self.clusterrole_name} and configmap/{self.configmap_name}",
            namespace=self.namespace,
            description=(
                f"ConfigMap `{self.configmap_name}` is stuck in Terminating with finalizer `{self.finalizer}`. "
                f"The finalizer is owned by Deployment `{self.controller_name}` using ServiceAccount "
                f"`{self.sa_name}`, but ClusterRole `{self.clusterrole_name}` was changed to read-only and is "
                "missing patch/update permissions for both `configmaps` and `configmaps/finalizers`. The "
                "controller pod repeatedly logs HTTP 403 Forbidden while trying to remove the finalizer. The "
                f"correct mitigation is to restore ClusterRole `{self.clusterrole_name}` so the controller can "
                "complete its normal reconcile loop, record cleanup completion on its Deployment, and let "
                "Kubernetes finish deleting the ConfigMap."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = FinalizerDeadlockControllerMitigationOracle(
            problem=self,
            configmap_name=self.configmap_name,
            finalizer=self.finalizer,
            clusterrole_name=self.clusterrole_name,
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
        self._start_controller_reconcile_loop()
        self._wait_for_forbidden_log()

        print(
            f"Resource: configmap/{self.configmap_name} | Controller: deployment/{self.controller_name} "
            f"| Namespace: {self.namespace}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        self._restore_clusterrole()
        if not self._wait_until_configmap_deleted(timeout_seconds=45):
            self._complete_controller_cleanup()
            self._wait_until_configmap_deleted(timeout_seconds=30)
        print(f"ClusterRole `{self.clusterrole_name}` restored; controller completed cleanup")

        print(f"Resource: configmap/{self.configmap_name} | Namespace: {self.namespace}\n")

    def _create_service_account(self):
        body = client.V1ServiceAccount(
            metadata=client.V1ObjectMeta(
                name=self.sa_name,
                namespace=self.namespace,
                labels={"platform.io/injected": "true"},
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
                labels={"platform.io/injected": "true"},
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
                labels={"platform.io/injected": "true"},
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
                "labels": {"platform.io/injected": "true"},
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
            f"""\
            #!/bin/sh
            set -eu

            echo "cleanup-controller starting namespace={self.namespace} configmap={self.configmap_name} finalizer={self.finalizer}"
            while true; do
              echo "ConfigMap {self.configmap_name} is Terminating with finalizer {self.finalizer}; attempting controller-owned cleanup"
              echo "cleanup-controller reconciliation failed: Kubernetes API denied the finalizer cleanup request with Forbidden"
              sleep 10
            done
            """
        )
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=_CONTROLLER_SCRIPT_CONFIGMAP,
                namespace=self.namespace,
                labels={"platform.io/injected": "true"},
            ),
            data={"loop.sh": script},
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
                labels={"app": self.controller_name, "platform.io/injected": "true"},
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
                                command=["/bin/sh", "/controller/loop.sh"],
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
                if "Kubernetes API denied the finalizer cleanup request with Forbidden" in logs:
                    return
            time.sleep(2)
        print("Controller 403 log not observed before timeout; fault state was still injected")

    def _start_controller_reconcile_loop(self):
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(
            target=self._controller_reconcile_loop,
            name=f"{self.controller_name}-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def _controller_reconcile_loop(self):
        while not self._reconcile_stop.is_set():
            try:
                if self._clusterrole_has_required_rules() and self._configmap_needs_finalizer_cleanup():
                    self._complete_controller_cleanup()
                    return
            except ApiException as e:
                if e.status not in {404, 409}:
                    print(f"cleanup-controller reconcile loop observed API error: {e}")
            except Exception as e:
                print(f"cleanup-controller reconcile loop observed error: {e}")
            self._reconcile_stop.wait(5)

    def _clusterrole_has_required_rules(self):
        try:
            role = self.rbac_v1.read_cluster_role(self.clusterrole_name, _request_timeout=10)
        except ApiException:
            return False

        verbs_by_resource = {"configmaps": set(), "configmaps/finalizers": set()}
        for rule in role.rules or []:
            resources = set(rule.resources or [])
            verbs = set(rule.verbs or [])
            for resource in verbs_by_resource:
                if resource in resources or "*" in resources:
                    verbs_by_resource[resource].update(verbs)

        required = {"patch", "update"}
        return all(required <= verbs for verbs in verbs_by_resource.values())

    def _configmap_needs_finalizer_cleanup(self):
        try:
            cm = self.kubectl.core_v1_api.read_namespaced_config_map(
                self.configmap_name,
                self.namespace,
                _request_timeout=10,
            )
        except ApiException:
            return False
        return bool(cm.metadata.deletion_timestamp and self.finalizer in (cm.metadata.finalizers or []))

    def _complete_controller_cleanup(self):
        self._remove_configmap_finalizers()
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            name=self.controller_name,
            namespace=self.namespace,
            body={
                "metadata": {
                    "annotations": {
                        _CONTROLLER_COMPLETION_ANNOTATION: time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(),
                        )
                    }
                }
            },
            _request_timeout=10,
        )

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
        self._reconcile_stop.set()
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
