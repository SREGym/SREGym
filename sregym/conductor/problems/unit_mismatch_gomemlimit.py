"""Unit-mismatch fault: GOMEMLIMIT set with a wrong byte-suffix.

Derived from the `unit-mismatch-interface-failure` template mined from public
postmortems. The canonical instance is Mars Climate Orbiter (metric vs
imperial); in software, Go's GOMEMLIMIT is an apt analog: accepted suffixes
are B/KiB/MiB/GiB/TiB, while operators frequently type the SI-style "MB"/"GB"
which the runtime rejects. The pod then crash-loops at startup with a parse
error, but the error is subtle — nothing else on the deployment looks wrong.
"""

from sregym.conductor.oracles.behavioral_probes import DeploymentStableOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


_CORRECT_VALUES = {
    "checkout": "16MiB",
    "product-catalog": "16MiB",
    "flagd": "60MiB",
}


class UnitMismatchGomemlimit(Problem):
    def __init__(
        self,
        app_name: str = "astronomy_shop",
        faulty_service: str = "checkout",
    ):
        self.app_name = app_name
        self.faulty_service = faulty_service

        if self.app_name != "astronomy_shop":
            raise ValueError(f"UnitMismatchGomemlimit only supports astronomy_shop, got {app_name}")
        if self.faulty_service not in _CORRECT_VALUES:
            raise ValueError(
                f"UnitMismatchGomemlimit has no variant for '{faulty_service}'; "
                f"known: {sorted(_CORRECT_VALUES)}"
            )

        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.env_var = "GOMEMLIMIT"
        self.correct_value = _CORRECT_VALUES[self.faulty_service]
        # SI-style MB is rejected by the Go runtime — the binary suffix MiB is required.
        self.wrong_value = self.correct_value.replace("MiB", "MB")

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.env_var} environment variable on the {self.faulty_service} "
                f"deployment is set to '{self.wrong_value}'. The Go runtime only accepts "
                "binary-SI byte suffixes (B, KiB, MiB, GiB, TiB); the SI-style 'MB' suffix "
                "is rejected at startup, so the pod crash-loops before it can serve traffic. "
                "Observable symptoms: checkout pods in CrashLoopBackOff, 5xx errors on "
                "frontend checkout attempts. The correct value is '16MiB' — a classic "
                "binary-vs-decimal unit mismatch."
            ),
        )

        self.kubectl = KubeCtl()
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = DeploymentStableOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.inject_env_value_override(
            deployment_name=self.faulty_service,
            env_var=self.env_var,
            wrong_value=self.wrong_value,
        )
        # Default RollingUpdate keeps the old Ready pod alive when the new
        # one crashloops, hiding the fault from any "ready replicas" check.
        # Force the new ReplicaSet's pod into view by deleting the old one.
        self.kubectl.exec_command(
            f"kubectl delete pod -l app.kubernetes.io/component={self.faulty_service} "
            f"-n {self.namespace} --ignore-not-found"
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = ApplicationFaultInjector(namespace=self.namespace)
        injector.recover_env_value_override(
            deployment_name=self.faulty_service,
            env_var=self.env_var,
            correct_value=self.correct_value,
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
