from kubernetes import client

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.network_policy_oracle import NetworkPolicyMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.paths import TARGET_MICROSERVICES
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class NetworkPolicyBlock(Problem):
    def __init__(self, faulty_service="payment-service"):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)
        self.kubectl = KubeCtl()
        self.faulty_service = faulty_service
        self.policy_name = f"deny-all-{faulty_service}"

        self.app.payload_script = (
            TARGET_MICROSERVICES / "hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua"
        )
        self.app.create_workload()

        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"A NetworkPolicy `{self.policy_name}` blocks all ingress and egress traffic for pods labeled "
                f"`app={self.faulty_service}`, creating complete network isolation for the target workload. "
                "Service-to-service communication to and from the component fails, so dependent request paths break "
                "even if pods remain Running. Users observe hard failures/timeouts on flows that require this service."
            ),
        )
        self.networking_v1 = client.NetworkingV1Api()

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = NetworkPolicyMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        """Block ALL traffic to/from the target service"""
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": self.policy_name, "namespace": self.namespace},
            "spec": {
                "podSelector": {"matchLabels": {"app": self.faulty_service}},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }
        self.networking_v1.create_namespaced_network_policy(namespace=self.namespace, body=policy)

    @mark_fault_injected
    def recover_fault(self):
        """Remove the NetworkPolicy"""
        self.networking_v1.delete_namespaced_network_policy(name=self.policy_name, namespace=self.namespace)
