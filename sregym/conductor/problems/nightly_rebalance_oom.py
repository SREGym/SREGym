"""Capacity-rebalance overshoot -> OOMKill under an observability blackout."""

import json
import time
from contextlib import contextmanager

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.nightly_rebalance_oom_mitigation import NightlyRebalanceOOMMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class NightlyRebalanceOOM(Problem):
    actor_name = "vpa-updater"
    actor_namespace = "kube-system"
    schedule = "* * * * *"
    squeeze_memory = "4Mi"

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "recommendation"):
        if app_name != "hotel_reservation":
            raise ValueError(f"Unsupported app name: {app_name}")
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = faulty_service
        self._target_container = None
        self._original_memory_limit = None

        self.root_cause = self.build_structured_root_cause(
            component=f"{self.actor_name} CronJob ({self.actor_namespace}) + deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"A scheduled capacity controller (`{self.actor_name}` CronJob in `{self.actor_namespace}`) "
                f"periodically patches deployment `{self.faulty_service}` memory limit down to `{self.squeeze_memory}`, "
                "below its startup working set, so the container is OOMKilled during init and stays in "
                "CrashLoopBackOff. The crashed pod emits no metrics or logs of its own. Raising the limit is reverted "
                f"on the controller's next tick; a durable fix must suspend or remove the `{self.actor_name}` CronJob "
                f"AND restore a sane memory limit on `{self.faulty_service}`."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = NightlyRebalanceOOMMitigationOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        container = dep.spec.template.spec.containers[0]
        self._target_container = container.name
        limits = (container.resources.limits or {}) if container.resources else {}
        self._original_memory_limit = limits.get("memory")

        self._delete_cronjob()  # clear any leftover actor from a crashed prior run (idempotent)
        self._create_rbac()
        self._create_cronjob()
        self._apply_squeeze()  # squeeze once now so we don't wait up to a minute for the first tick
        self._wait_for_target_unhealthy(timeout=180)
        print(
            f"Service: {self.faulty_service} | squeeze={self.squeeze_memory} | actor={self.actor_namespace}/{self.actor_name}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._delete_cronjob()
        time.sleep(5)  # let any in-flight tick finish before restoring
        self._delete_rbac()
        self._restore_memory_limit()
        print(f"Recovered: removed {self.actor_name} CronJob and restored deployment/{self.faulty_service}\n")

    def _squeeze_patch(self) -> dict:
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": self._target_container, "resources": {"limits": {"memory": self.squeeze_memory}}}
                        ]
                    }
                }
            }
        }

    def _apply_squeeze(self):
        self.kubectl.patch_deployment(self.faulty_service, self.namespace, self._squeeze_patch())

    def _wait_for_target_unhealthy(self, timeout: int):
        unhealthy_reasons = {
            "CrashLoopBackOff",
            "CreateContainerError",
            "RunContainerError",
            "CreateContainerConfigError",
            "Error",
            "ImagePullBackOff",
            "ErrImagePull",
        }
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            pods = self.kubectl.list_pods(self.namespace).items
            target = [p for p in pods if (p.metadata.labels or {}).get("io.kompose.service") == self.faulty_service]
            for pod in target:
                for cs in pod.status.container_statuses or []:
                    waiting = cs.state.waiting.reason if cs.state.waiting else None
                    terminated = cs.state.terminated.reason if cs.state.terminated else None
                    last = f"{pod.metadata.name}: waiting={waiting} terminated={terminated} restarts={cs.restart_count}"
                    if waiting in unhealthy_reasons or terminated == "OOMKilled" or (cs.restart_count or 0) >= 1:
                        print(f"Fault confirmed live: {last}")
                        return
            print(f"Waiting for squeeze to take effect... {last}")
            time.sleep(5)
        raise RuntimeError(f"Target {self.faulty_service} did not become unhealthy in time; last={last}")

    def _restore_memory_limit(self):
        if self._target_container is None:
            return
        limits = {"memory": self._original_memory_limit or "256Mi"}
        patch = {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": self._target_container, "resources": {"limits": limits}}]}
                }
            }
        }
        self.kubectl.patch_deployment(self.faulty_service, self.namespace, patch)

    def _create_cronjob(self):
        patch_json = json.dumps(self._squeeze_patch(), separators=(",", ":"))
        cmd = f"kubectl -n {self.namespace} patch deployment {self.faulty_service} --type=strategic -p '{patch_json}'"
        body = {
            "metadata": {
                "name": self.actor_name,
                "namespace": self.actor_namespace,
                "labels": {"app": self.actor_name},
            },
            "spec": {
                "schedule": self.schedule,
                "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 1,
                "failedJobsHistoryLimit": 1,
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"app": self.actor_name}},
                            "spec": {
                                "serviceAccountName": self.actor_name,
                                "restartPolicy": "Never",
                                "containers": [
                                    {"name": "patch", "image": "bitnami/kubectl:latest", "command": ["sh", "-c", cmd]}
                                ],
                            },
                        }
                    }
                },
            },
        }
        client.BatchV1Api().create_namespaced_cron_job(self.actor_namespace, body)

    def _delete_cronjob(self):
        batch = client.BatchV1Api()
        with self._ignore_not_found():
            batch.delete_namespaced_cron_job(self.actor_name, self.actor_namespace, propagation_policy="Background")
        with self._ignore_not_found():
            batch.delete_collection_namespaced_job(self.actor_namespace, label_selector=f"app={self.actor_name}")

    def _create_rbac(self):
        core, rbac = self.kubectl.core_v1_api, client.RbacAuthorizationV1Api()
        with self._ignore_conflict():
            core.create_namespaced_service_account(self.actor_namespace, {"metadata": {"name": self.actor_name}})
        with self._ignore_conflict():
            rbac.create_namespaced_role(
                self.namespace,
                {
                    "metadata": {"name": self.actor_name},
                    "rules": [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "patch"]}],
                },
            )
        with self._ignore_conflict():
            rbac.create_namespaced_role_binding(
                self.namespace,
                {
                    "metadata": {"name": self.actor_name},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": self.actor_name},
                    "subjects": [
                        {"kind": "ServiceAccount", "name": self.actor_name, "namespace": self.actor_namespace}
                    ],
                },
            )

    def _delete_rbac(self):
        core, rbac = self.kubectl.core_v1_api, client.RbacAuthorizationV1Api()
        with self._ignore_not_found():
            rbac.delete_namespaced_role_binding(self.actor_name, self.namespace)
        with self._ignore_not_found():
            rbac.delete_namespaced_role(self.actor_name, self.namespace)
        with self._ignore_not_found():
            core.delete_namespaced_service_account(self.actor_name, self.actor_namespace)

    @contextmanager
    def _ignore_not_found(self):
        try:
            yield
        except ApiException as e:
            if e.status != 404:
                raise

    @contextmanager
    def _ignore_conflict(self):
        try:
            yield
        except ApiException as e:
            if e.status != 409:
                raise