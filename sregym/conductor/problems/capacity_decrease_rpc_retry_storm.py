from sregym.conductor.oracles.llm_as_a_judge.mock_llm_as_a_judge_oracle import MockLLMAsAJudgeOracle
from sregym.conductor.oracles.rpc_retry_storm_mitigation import RPCRetryStormMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.generators.workload.blueprint_hotel_work import BHotelWrk, BHotelWrkWorkloadManager
from sregym.service.apps.blueprint_hotel_reservation import BlueprintHotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class CapacityDecreaseRPCRetryStorm(Problem):
    def __init__(self):
        self.app = BlueprintHotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
        self.faulty_service = "rpc"
        self.root_cause = "The ConfigMap rpc has misconfigured RPC timeout (50ms) and retry settings (30 retries), causing an RPC retry storm that overwhelms the service. It is a metastable failure."
        # === Attach evaluation oracles ===
        self.diagnosis_oracle = MockLLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.mitigation_oracle = RPCRetryStormMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.inject_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
        # ms failure should persist after removing the trigger
        config_patch_command = f'kubectl patch configmap rpc -n {self.namespace} -p \'{{"data":{{"GRPC_CLIENT_TIMEOUT":"1s","GRPC_CLIENT_RETRIES_ON_ERROR":"1"}}}}\''
        self.kubectl.exec_command(config_patch_command)
        print(self.mitigation_oracle.run_workload(problem=self, kubectl=self.kubectl))

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector.recover_rpc_timeout_retries_misconfiguration(configmap=self.faulty_service)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def create_workload(self, tput: int = None, duration: str = None, multiplier: int = None):
        if tput is None:
            tput = 3000
        if duration is None:
            duration = "500s"
        if multiplier is None:
            multiplier = 1
        self.wrk = BHotelWrkWorkloadManager(
            wrk=BHotelWrk(tput=tput, duration=duration, multiplier=multiplier),
            CPU_containment=True,
        )

    def start_workload(self):
        if not hasattr(self, "wrk"):
            self.create_workload()
        self.wrk.start()

    def stop_workload(self):
        if hasattr(self, "wrk"):
            self.wrk.stop()


def main():
    prob = CapacityDecreaseRPCRetryStorm()
    print("====== manual ======")
    print(prob.recover_fault())
    print("get pure latencies")
    print(prob.mitigation_oracle.run_workload(problem=prob, kubectl=prob.kubectl))


if __name__ == "__main__":
    main()
