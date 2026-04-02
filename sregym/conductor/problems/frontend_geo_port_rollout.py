import json

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.frontend_geo_port_rollout_oracle import FrontendGeoPortRolloutMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class FrontendGeoPortRollout(Problem):
    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.kubectl = KubeCtl()
        self.faulty_service = "frontend"
        self.deployment_name = "frontend"
        self.pod_label_selector = "io.kompose.service=frontend"
        self.config_path = "/go/src/github.com/harlow/go-micro-services/config.json"
        self.configmap_name = "frontend-runtime-config"
        self.volume_name = "frontend-runtime-config-volume"
        self.volume_mount_name = self.volume_name
        self.expected_geo_port = 8083
        self.wrong_geo_port = 18083
        self.faulty_frontend_pod_uid: str | None = None

        self.root_cause = self.build_structured_root_cause(
            component=f"configmap/{self.configmap_name}",
            namespace=self.namespace,
            description=(
                f"Frontend deployment `{self.deployment_name}` loads `GeoPort` from mounted config file "
                f"`{self.config_path}` at startup. The frontend ConfigMap drifted so `GeoPort` is "
                f"`{self.wrong_geo_port}` instead of `{self.expected_geo_port}`. Search and recommendation "
                "requests sent through the frontend now target the wrong backend port, while the frontend root "
                "still serves traffic. Restoring service correctness requires fixing the frontend config and "
                "restarting the single frontend pod so it reloads the repaired file."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.resolution_oracle = FrontendGeoPortRolloutMitigationOracle(problem=self)
        self.mitigation_oracle = AlertOracle(problem=self)

        self.app.create_workload()

    def _get_frontend_runtime_config(self) -> dict:
        pod_name = self.kubectl.get_pod_name(self.namespace, self.pod_label_selector)
        raw = self.kubectl.exec_command(f"kubectl exec -n {self.namespace} {pod_name} -- cat {self.config_path}")
        return json.loads(raw)

    def _ensure_frontend_runtime_configmap(self, geo_port: int) -> None:
        config_data = self._get_frontend_runtime_config()
        config_data["GeoPort"] = str(geo_port)
        self.kubectl.create_or_update_configmap(
            name=self.configmap_name,
            namespace=self.namespace,
            data={"service-config.json": json.dumps(config_data, indent=4, sort_keys=False)},
        )

    def _ensure_frontend_deployment_mounts_runtime_config(self) -> None:
        volume_names = self.kubectl.exec_command(
            f"kubectl get deployment {self.deployment_name} -n {self.namespace} "
            "-o jsonpath='{.spec.template.spec.volumes[*].name}'"
        )
        mount_names = self.kubectl.exec_command(
            f"kubectl get deployment {self.deployment_name} -n {self.namespace} "
            "-o jsonpath='{.spec.template.spec.containers[0].volumeMounts[*].name}'"
        )

        patch_ops = []
        if self.volume_name not in volume_names.split():
            volumes_exist = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name} -n {self.namespace} "
                "-o jsonpath='{.spec.template.spec.volumes}'"
            ).strip()
            volume_op = {
                "op": "add",
                "path": "/spec/template/spec/volumes/-",
                "value": {"name": self.volume_name, "configMap": {"name": self.configmap_name}},
            }
            if not volumes_exist or volumes_exist == "[]":
                volume_op["path"] = "/spec/template/spec/volumes"
                volume_op["value"] = [volume_op["value"]]
            patch_ops.append(volume_op)

        if self.volume_mount_name not in mount_names.split():
            mounts_exist = self.kubectl.exec_command(
                f"kubectl get deployment {self.deployment_name} -n {self.namespace} "
                "-o jsonpath='{.spec.template.spec.containers[0].volumeMounts}'"
            ).strip()
            mount_op = {
                "op": "add",
                "path": "/spec/template/spec/containers/0/volumeMounts/-",
                "value": {
                    "name": self.volume_mount_name,
                    "mountPath": self.config_path,
                    "subPath": "service-config.json",
                },
            }
            if not mounts_exist or mounts_exist == "[]":
                mount_op["path"] = "/spec/template/spec/containers/0/volumeMounts"
                mount_op["value"] = [mount_op["value"]]
            patch_ops.append(mount_op)

        if patch_ops:
            patch_json = json.dumps(patch_ops)
            self.kubectl.exec_command(
                f"kubectl patch deployment {self.deployment_name} -n {self.namespace} --type='json' -p='{patch_json}'"
            )

    def _patch_frontend_configmap_geo_port(self, geo_port: int) -> None:
        configmap = self.kubectl.core_v1_api.read_namespaced_config_map(self.configmap_name, self.namespace)
        config_data = json.loads((configmap.data or {}).get("service-config.json", "{}"))
        config_data["GeoPort"] = str(geo_port)
        patch = {"data": {"service-config.json": json.dumps(config_data, indent=4, sort_keys=False)}}
        self.kubectl.core_v1_api.patch_namespaced_config_map(
            name=self.configmap_name,
            namespace=self.namespace,
            body=patch,
        )

    def _rollout_frontend(self) -> None:
        self.kubectl.trigger_rollout(self.deployment_name, self.namespace)
        self.kubectl.wait_for_ready(self.namespace, service_names=self.deployment_name)

    def _get_frontend_pod_uid(self) -> str:
        pod_name = self.kubectl.get_pod_name(self.namespace, self.pod_label_selector)
        pod = self.kubectl.core_v1_api.read_namespaced_pod(pod_name, self.namespace)
        return pod.metadata.uid

    @mark_fault_injected
    def inject_fault(self):
        self._ensure_frontend_runtime_configmap(self.wrong_geo_port)
        self._ensure_frontend_deployment_mounts_runtime_config()
        self._rollout_frontend()
        self.faulty_frontend_pod_uid = self._get_frontend_pod_uid()

    @mark_fault_injected
    def recover_fault(self):
        self._patch_frontend_configmap_geo_port(self.expected_geo_port)
        self._rollout_frontend()
