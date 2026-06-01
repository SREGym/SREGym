import json

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.workload import WorkloadOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

SYSCTL_NAME = "net.ipv4.ip_local_port_range"
BAD_RANGE = "32000 32003"
DEFAULT_RANGE = "32768 60999"


class EphemeralPortRangeHotelReservation(Problem):
    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.faulty_service = "all"

        self.root_cause = self.build_structured_root_cause(
            component="all hotel-reservation deployments",
            namespace=self.namespace,
            description=(
                f"All hotel-reservation pods set {SYSCTL_NAME} to '{BAD_RANGE}', "
                "which narrows the ephemeral port range to 4 ports. The frontend "
                "cannot establish outbound TCP connections to all gRPC backends; "
                "at least one backend is always unreachable with EADDRNOTAVAIL "
                "even though pods remain Running."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        # Workload checks catch failures even when pods stay Running.
        self.app.create_workload()
        self.mitigation_oracle = WorkloadOracle(problem=self, wrk_manager=self.app.wrk)

    # These are not microservice peers — patching them causes Consul to lose
    # service registrations on restart, breaking discovery for app services.
    _SYSCTL_EXCLUDE = frozenset({"consul", "jaeger"})

    def _patch_sysctl(self, value: str | None):
        deployments = [
            d
            for d in self.kubectl.list_deployments(self.namespace).items
            if d.metadata.name not in self._SYSCTL_EXCLUDE
        ]
        if not deployments:
            raise RuntimeError("No deployments found to patch sysctl.")

        if value is None:
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "securityContext": {
                                "sysctls": None,
                            }
                        }
                    }
                }
            }
        else:
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "securityContext": {
                                "sysctls": [
                                    {
                                        "name": SYSCTL_NAME,
                                        "value": value,
                                    }
                                ]
                            }
                        }
                    }
                }
            }

        payload = json.dumps(patch)
        for dep in deployments:
            name = dep.metadata.name
            self.kubectl.exec_command(
                f"kubectl -n {self.namespace} patch deployment {name} --type=merge -p '{payload}'"
            )

    def _wait_for_rollouts(self):
        deployments = self.kubectl.list_deployments(self.namespace).items
        for dep in deployments:
            name = dep.metadata.name
            self.kubectl.exec_command(f"kubectl -n {self.namespace} rollout status deployment/{name} --timeout=300s")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._patch_sysctl(BAD_RANGE)
        self._wait_for_rollouts()

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        # Restore the default range explicitly for deterministic recovery.
        self._patch_sysctl(DEFAULT_RANGE)
        self._wait_for_rollouts()
