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
    app_namespace = "hotel-reservation"
    policy_configmap = "vpa-updater-policy"
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
            component=f"{self.actor_name} CronJob ({self.actor_namespace})",
            namespace=self.actor_namespace,
            description=(
                f"The fault originates from the scheduled `{self.actor_name}` CronJob in `{self.actor_namespace}`, "
                f"which periodically patches deployment `{self.faulty_service}` memory limit down to "
                f"`{self.squeeze_memory}`. The `{self.faulty_service}` deployment is the affected victim: that "
                "limit is below its startup working set, so the container is OOMKilled during init and stays in "
                "CrashLoopBackOff. A diagnosis that only names the deployment's OOMKilled pod or bad memory limit "
                f"without identifying the recurring `{self.actor_name}` actor is incomplete. A durable fix must "
                f"suspend/remove the `{self.actor_name}` CronJob or correct its policy, and restore a sane memory "
                f"limit on `{self.faulty_service}`."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = NightlyRebalanceOOMMitigationOracle(problem=self)
        self.app.create_workload()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        containers = dep.spec.template.spec.containers
        container = next((c for c in containers if c.name == self.faulty_service), containers[0])
        self._target_container = container.name
        limits = (container.resources.limits or {}) if container.resources else {}
        self._original_memory_limit = limits.get("memory")

        self._teardown_actor()
        self._create_rbac()
        self._create_policy_configmap()
        self._create_cronjob()
        self._apply_squeeze()
        self._wait_for_target_unhealthy(timeout=180)
        print(
            f"Service: {self.faulty_service} | squeeze={self.squeeze_memory} | actor={self.actor_namespace}/{self.actor_name}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._teardown_actor()
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
        unhealthy_reasons = {"CrashLoopBackOff", "CreateContainerError"}
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
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        for container in dep.spec.template.spec.containers:
            if container.name != self._target_container:
                continue
            if container.resources is None:
                container.resources = client.V1ResourceRequirements()
            limits = container.resources.limits or {}
            if self._original_memory_limit is not None:
                limits["memory"] = self._original_memory_limit
            else:
                limits.pop("memory", None)
            container.resources.limits = limits or None
        self.kubectl.update_deployment(self.faulty_service, self.namespace, dep)

    def _create_policy_configmap(self):
        body = {
            "metadata": {"name": self.policy_configmap, "labels": {"app": self.actor_name}},
            "data": {
                "NAMESPACE": self.namespace,
                "TARGET": self.faulty_service,
                "PATCH": json.dumps(self._squeeze_patch(), separators=(",", ":")),
            },
        }
        with self._ignore_conflict():
            self.kubectl.core_v1_api.create_namespaced_config_map(self.actor_namespace, body)

    def _create_cronjob(self):
        cmd = 'kubectl -n "$NAMESPACE" patch deployment "$TARGET" --type=strategic -p "$PATCH"'
        container = {
            "name": "patch",
            "image": "bitnami/kubectl:latest",
            "command": ["sh", "-c", cmd],
            "envFrom": [{"configMapRef": {"name": self.policy_configmap}}],
        }
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
                    "metadata": {"labels": {"app": self.actor_name}},
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"app": self.actor_name}},
                            "spec": {
                                "serviceAccountName": self.actor_name,
                                "restartPolicy": "Never",
                                "containers": [container],
                            },
                        }
                    },
                },
            },
        }
        client.BatchV1Api().create_namespaced_cron_job(self.actor_namespace, body)

    def _teardown_actor(self):
        self._delete_cron_and_jobs()
        self._wait_for_actor_gone(timeout=90)
        self._delete_actor_config_and_rbac()

    def _delete_cron_and_jobs(self):
        batch = client.BatchV1Api()
        with self._ignore_not_found():
            batch.delete_namespaced_cron_job(self.actor_name, self.actor_namespace, propagation_policy="Foreground")
        with self._ignore_not_found():
            batch.delete_collection_namespaced_job(
                self.actor_namespace, label_selector=f"app={self.actor_name}", propagation_policy="Foreground"
            )

    def _delete_actor_config_and_rbac(self):
        core, rbac = self.kubectl.core_v1_api, client.RbacAuthorizationV1Api()
        with self._ignore_not_found():
            core.delete_namespaced_config_map(self.policy_configmap, self.actor_namespace)
        with self._ignore_not_found():
            rbac.delete_namespaced_role_binding(self.actor_name, self.namespace)
        with self._ignore_not_found():
            rbac.delete_namespaced_role(self.actor_name, self.namespace)
        with self._ignore_not_found():
            core.delete_namespaced_service_account(self.actor_name, self.actor_namespace)

    @classmethod
    def _wait_for_actor_gone(cls, timeout: int = 90):
        batch, core = client.BatchV1Api(), client.CoreV1Api()
        label = f"app={cls.actor_name}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cron_gone = False
            try:
                batch.read_namespaced_cron_job(cls.actor_name, cls.actor_namespace)
            except ApiException as e:
                if e.status != 404:
                    raise
                cron_gone = True
            jobs = batch.list_namespaced_job(cls.actor_namespace, label_selector=label).items
            pods = core.list_namespaced_pod(cls.actor_namespace, label_selector=label).items
            if cron_gone and not jobs and not pods:
                return
            time.sleep(3)
        print(f"Actor {cls.actor_namespace}/{cls.actor_name} not fully gone after {timeout}s; proceeding")

    @classmethod
    def cleanup_leftover_actor(cls):
        batch, core = client.BatchV1Api(), client.CoreV1Api()
        rbac = client.RbacAuthorizationV1Api()

        def ignore_404(fn, *args, **kwargs):
            try:
                fn(*args, **kwargs)
            except ApiException as e:
                if e.status != 404:
                    raise

        ignore_404(
            batch.delete_namespaced_cron_job, cls.actor_name, cls.actor_namespace, propagation_policy="Foreground"
        )
        ignore_404(
            batch.delete_collection_namespaced_job,
            cls.actor_namespace,
            label_selector=f"app={cls.actor_name}",
            propagation_policy="Foreground",
        )
        cls._wait_for_actor_gone(timeout=90)
        ignore_404(core.delete_namespaced_config_map, cls.policy_configmap, cls.actor_namespace)
        ignore_404(core.delete_namespaced_service_account, cls.actor_name, cls.actor_namespace)
        ignore_404(rbac.delete_namespaced_role_binding, cls.actor_name, cls.app_namespace)
        ignore_404(rbac.delete_namespaced_role, cls.actor_name, cls.app_namespace)

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
