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
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
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
            core_v1.delete_namespaced_config_map(self.configmap_name, self.namespace)
        except ApiException as e:
            if e.status != 404:
                raise

        self._wait_until_configmap_deleted()

        try:
            core_v1.create_namespaced_config_map(namespace=self.namespace, body=body)
        except ApiException as e:
            if e.status == 409:
                self._clear_finalizer_if_present()
                self._wait_until_configmap_deleted()
                core_v1.create_namespaced_config_map(namespace=self.namespace, body=body)
            else:
                raise

    def _clear_finalizer_if_present(self):
        try:
            self.kubectl.exec_command(
                f"kubectl patch configmap {self.configmap_name} -n {self.namespace} "
                '--type=merge -p \'{"metadata":{"finalizers":[]}}\''
            )
        except Exception:
            # Best-effort cleanup: the ConfigMap may already be gone.
            return

    def _wait_until_configmap_deleted(self, timeout_seconds: int = 30):
        core_v1 = self.kubectl.core_v1_api
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            try:
                core_v1.read_namespaced_config_map(self.configmap_name, self.namespace)
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
            f"kubectl delete configmap {self.configmap_name} -n {self.namespace} --ignore-not-found"
        )
        print(f"Resource: configmap/{self.configmap_name} | Namespace: {self.namespace}\n")
