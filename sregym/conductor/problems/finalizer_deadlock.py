"""Kubernetes finalizer deadlock problem."""

import time

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.finalizer_deadlock_mitigation import FinalizerDeadlockMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FinalizerDeadlock(Problem):
    def __init__(self):
        app = HotelReservation()
        super().__init__(app=app, namespace=app.namespace)

        self.kubectl = KubeCtl()
        self.configmap_name = "reservation-cleanup-token"
        self.finalizer = "cleanup.reservations.io/pending-cleanup"
        self.faulty_service = self.configmap_name
        self.root_cause = self.build_structured_root_cause(
            component=f"configmap/{self.configmap_name}",
            namespace=self.namespace,
            description=(
                f"ConfigMap `{self.configmap_name}` has a deletion timestamp but remains stuck in Terminating "
                f"because it contains orphaned finalizer `{self.finalizer}`. The controller that should remove "
                "the finalizer does not exist, so Kubernetes will not complete object deletion until the dangling "
                "finalizer is safely removed."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = FinalizerDeadlockMitigationOracle(
            problem=self,
            configmap_name=self.configmap_name,
            finalizer=self.finalizer,
        )

    def _create_finalized_configmap(self):
        self._delete_existing_configmap()
        self._wait_until_configmap_deleted()
        self._create_configmap_with_finalizer()

    def _delete_existing_configmap(self):
        core_v1 = self.kubectl.core_v1_api

        try:
            core_v1.delete_namespaced_config_map(self.configmap_name, self.namespace, _request_timeout=10)
        except ApiException as e:
            if e.status != 404:
                raise

    def _create_configmap_with_finalizer(self):
        core_v1 = self.kubectl.core_v1_api
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
            core_v1.create_namespaced_config_map(namespace=self.namespace, body=body, _request_timeout=10)
        except ApiException as e:
            if e.status == 409:
                self._clear_finalizer_if_present()
                self._wait_until_configmap_deleted()
                core_v1.create_namespaced_config_map(namespace=self.namespace, body=body, _request_timeout=10)
            else:
                raise

    def _clear_finalizer_if_present(self):
        try:
            configmap = self.kubectl.core_v1_api.read_namespaced_config_map(
                self.configmap_name,
                self.namespace,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise

        if not (configmap.metadata.finalizers or []):
            return

        output = self.kubectl.exec_command(
            f"kubectl patch configmap {self.configmap_name} -n {self.namespace} "
            '--type=json -p \'[{"op":"remove","path":"/metadata/finalizers"}]\' --request-timeout=10s'
        )
        output_lower = output.lower()

        if "not found" in output_lower:
            return

        if "patched" not in output_lower:
            raise RuntimeError(f"Failed to clear finalizer from ConfigMap {self.configmap_name}: {output.strip()}")

        try:
            configmap = self.kubectl.core_v1_api.read_namespaced_config_map(
                self.configmap_name,
                self.namespace,
                _request_timeout=10,
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise

        if not (configmap.metadata.finalizers or []):
            return

        raise RuntimeError(f"Failed to clear finalizer from ConfigMap {self.configmap_name}: {output.strip()}")

    def _wait_until_configmap_deleted(self, timeout_seconds: int = 30):
        core_v1 = self.kubectl.core_v1_api
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            try:
                core_v1.read_namespaced_config_map(self.configmap_name, self.namespace, _request_timeout=10)
            except ApiException as e:
                if e.status == 404:
                    return
                raise

            self._clear_finalizer_if_present()
            time.sleep(1)

        raise TimeoutError(f"Timed out waiting for ConfigMap {self.configmap_name} to be deleted")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._create_finalized_configmap()
        self.kubectl.exec_command(f"kubectl delete configmap {self.configmap_name} -n {self.namespace} --wait=false")
        print(f"Resource: configmap/{self.configmap_name} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._clear_finalizer_if_present()
        self.kubectl.exec_command(
            f"kubectl delete configmap {self.configmap_name} -n {self.namespace} "
            "--ignore-not-found --wait=false --request-timeout=10s"
        )
        print(f"Resource: configmap/{self.configmap_name} | Namespace: {self.namespace}\n")
