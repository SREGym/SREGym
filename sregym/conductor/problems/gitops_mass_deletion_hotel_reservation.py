"""
Real-world failure simulation based on:
"How a couple of characters brought down our site"
Skyscanner Engineering - November 2021
https://medium.com/@SkyscannerEng/how-a-couple-of-characters-brought-down-our-site-356ccaf1fbc3

INCIDENT SUMMARY:
On August 25, 2021, Skyscanner suffered a 4.5-hour global outage affecting
all travellers and partners worldwide. An engineer submitted what was intended
to be a no-op change to the infrastructure templating system. The missing
{{ }} characters meant templating no longer applied, corrupting all namespace
configurations. ArgoCD then reconciled the corrupted config and mass-deleted
all 478 microservices across all namespaces, regions and availability zones.

KEY LESSON:
"When you use templates and logic in configuration, it becomes code."
A single character difference in a config file can trigger automated systems
to destroy an entire production environment.

SIMULATION:
We simulate this by:
1. Injecting a corrupted "cluster-config" ConfigMap into the namespace,
   representing the bad template that triggered the incident.
2. Immediately scaling ALL hotel-reservation deployments to 0 replicas,
   simulating ArgoCD's mass deletion of all services after reconciliation.
3. The agent observes: all pods gone, a suspicious ConfigMap present.
4. The agent must: identify the corrupted ConfigMap as root cause AND
   restore all deployments to their correct replica counts.

MITIGATION:
- Delete or fix the corrupted cluster-config ConfigMap
- Restore all scaled-down deployments to replicas=1
- All pods must return to Running state
"""

import time

from sregym.conductor.oracles.gitops_mass_deletion_mitigation import GitOpsMassDeletionMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# Generic name - does NOT reveal the fault category to the agent
_BAD_CONFIGMAP_NAME = "cluster-config"


class GitOpsMassDeletionHotelReservation(Problem):
    """
    Simulates a GitOps misconfiguration that causes mass deletion of all
    microservices in the hotel-reservation namespace.

    Based on the Skyscanner global outage (August 2021) where a single
    templating error in infrastructure config caused ArgoCD to delete
    all 478 production services worldwide.

    Fault: A corrupted 'cluster-config' ConfigMap is injected, and all
    deployments are scaled to 0 replicas (simulating ArgoCD mass deletion).

    The agent must identify the corrupted ConfigMap as the root cause and
    restore all deployments to bring the system back to a healthy state.
    """

    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace

        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()

        # Store original replica counts so we can restore them
        self._original_replicas = {}

        self.root_cause = self.build_structured_root_cause(
            component=f"configmap/{_BAD_CONFIGMAP_NAME}",
            namespace=self.namespace,
            description=(
                f"A corrupted infrastructure configuration ConfigMap named '{_BAD_CONFIGMAP_NAME}' "
                f"was applied to the '{self.namespace}' namespace. "
                "This simulates a GitOps reconciliation error where a bad template config "
                "caused the automated deployment system to scale all microservice deployments "
                "to zero replicas, effectively deleting all running services. "
                "All pods in the namespace terminated simultaneously, causing a complete "
                "service outage. The root cause is the corrupted ConfigMap which triggered "
                "the automated mass-deletion of all deployments."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = GitOpsMassDeletionMitigationOracle(problem=self)

    def _get_all_deployments(self):
        """Get all deployment names and their current replica counts."""
        deployments = self.kubectl.list_deployments(self.namespace)
        result = {}
        for dep in deployments.items:
            name = dep.metadata.name
            replicas = dep.spec.replicas or 1
            result[name] = replicas
        return result

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        # Step 1: Save original replica counts before deleting
        print("Saving deployment state before fault injection...")
        self._original_replicas = self._get_all_deployments()
        print(f"Found {len(self._original_replicas)} deployments to scale down")

        # Step 2: Inject the corrupted cluster-config ConfigMap
        # This represents the bad template that triggered the Skyscanner incident
        corrupted_config = {
            "cluster.config": (
                "# CORRUPTED CONFIGURATION - templating failed\n"
                "# Missing {{ }} caused all namespace configs to be invalidated\n"
                "namespaces: []\n"
                "services: []\n"
                "replicas: 0\n"
            ),
            "reconcile.policy": "delete-all-on-drift",
            "template.error": "true",
        }

        self.kubectl.create_or_update_configmap(
            name=_BAD_CONFIGMAP_NAME,
            namespace=self.namespace,
            data=corrupted_config,
        )
        print(f"Injected corrupted ConfigMap: {_BAD_CONFIGMAP_NAME}")

        # Step 3: Scale ALL deployments to 0 — simulating ArgoCD mass deletion
        print("Scaling all deployments to 0 (simulating GitOps mass deletion)...")
        for dep_name in self._original_replicas:
            self.kubectl.scale_deployment(
                name=dep_name,
                namespace=self.namespace,
                replicas=0,
            )
            print(f"  Scaled down: {dep_name}")

        print(f"\nAll {len(self._original_replicas)} deployments scaled to 0")
        print(f"Namespace: {self.namespace}")
        print("Symptom: All pods terminating — complete service outage\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # Step 1: Delete the corrupted ConfigMap
        self.kubectl.exec_command(
            f"kubectl delete configmap {_BAD_CONFIGMAP_NAME} -n {self.namespace} --ignore-not-found"
        )
        print(f"Deleted corrupted ConfigMap: {_BAD_CONFIGMAP_NAME}")

        # Step 2: Restore all deployments to original replica counts
        if not self._original_replicas:
            # Fallback: restore all to 1 replica if we lost state
            deployments = self.kubectl.list_deployments(self.namespace)
            for dep in deployments.items:
                self.kubectl.scale_deployment(
                    name=dep.metadata.name,
                    namespace=self.namespace,
                    replicas=1,
                )
                print(f"  Restored: {dep.metadata.name} -> 1 replica")
        else:
            for dep_name, replicas in self._original_replicas.items():
                self.kubectl.scale_deployment(
                    name=dep_name,
                    namespace=self.namespace,
                    replicas=replicas,
                )
                print(f"  Restored: {dep_name} -> {replicas} replica(s)")

        print(f"\nAll deployments restored in namespace: {self.namespace}")
        print("Waiting for pods to become ready...")
        time.sleep(10)
