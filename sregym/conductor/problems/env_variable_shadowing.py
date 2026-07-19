import copy

from sregym.conductor.oracles.env_variable_shadowing_mitigation import EnvVariableShadowingMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class EnvVariableShadowing(Problem):
    ENV_NAME = "FRONTEND_HOST"
    EXPECTED_VALUE = "frontend"
    SHADOW_VALUE = "localhost"

    def __init__(self, faulty_service: str = "frontend-proxy"):
        self.faulty_service = faulty_service

        super().__init__(app=AstronomyShop())
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"Container `{self.faulty_service}` defines `{self.ENV_NAME}` twice. The later value "
                f"`{self.SHADOW_VALUE}` shadows the earlier intended value `{self.EXPECTED_VALUE}`, so the edge proxy "
                "routes requests to the wrong upstream even though its pod remains Running and Ready."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = EnvVariableShadowingMitigationOracle(problem=self)
        self._baseline_template = None

    def _capture_baseline_template(self):
        if self._baseline_template is not None:
            return

        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        self._baseline_template = copy.deepcopy(deployment.spec.template)

    def _target_container_and_env(self, deployment):
        for container_index, container in enumerate(deployment.spec.template.spec.containers):
            if container.name == self.faulty_service:
                return container_index, container.env or []
        raise RuntimeError(f"Container '{self.faulty_service}' was not found")

    def _append_shadowing_definition(self):
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        container_index, environment = self._target_container_and_env(deployment)
        definitions = [item for item in environment if item.name == self.ENV_NAME]
        if len(definitions) != 1 or definitions[0].value != self.EXPECTED_VALUE:
            raise RuntimeError(
                f"Expected exactly one {self.ENV_NAME}={self.EXPECTED_VALUE} definition before injection"
            )

        patch = [
            {
                "op": "add",
                "path": f"/spec/template/spec/containers/{container_index}/env/-",
                "value": {"name": self.ENV_NAME, "value": self.SHADOW_VALUE},
            }
        ]
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=patch,
        )

    def _restore_baseline_template(self):
        if self._baseline_template is None:
            raise RuntimeError("Cannot recover environment shadowing without a captured baseline template")

        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        deployment.spec.template = copy.deepcopy(self._baseline_template)
        self.kubectl.apps_v1_api.replace_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=deployment,
        )

    def _wait_for_rollout(self):
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=120s"
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._capture_baseline_template()
        self._append_shadowing_definition()
        self._wait_for_rollout()
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._restore_baseline_template()
        self._wait_for_rollout()
        self._baseline_template = None
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}")
