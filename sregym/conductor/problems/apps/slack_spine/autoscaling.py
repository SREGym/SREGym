"""Slack Spine: autoscaling."""

from __future__ import annotations

import json
import time

from sregym.conductor.oracles.hpa_control_plane_mitigation import HPAControlPlaneMitigationOracle
from sregym.conductor.problems.environment import configure_port
from sregym.conductor.problems.hpa_missing_effective_cpu_request import HPAMissingEffectiveCPURequest


class HPAMissingEffectiveCPURequestSlack(HPAMissingEffectiveCPURequest):
    """A CPU-utilization HPA on svc-message whose pods lost their CPU request (and limit).

    As in the original, the pods stay Running/Ready: the fault breaks the
    autoscaling control loop, not the serving path, so the load generator is
    not expected to see errors while it is in place.
    """

    HPA_NAME = "svc-message-capacity"

    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        configure_port(
            self,
            app_name,
            component=f"Deployment/{faulty_service} resource configuration and HorizontalPodAutoscaler/{self.HPA_NAME}",
            description=(
                f"The message-send autoscaling loop is broken: `Deployment/{faulty_service}` produces pods without "
                f"an effective CPU request (both `requests.cpu` and `limits.cpu` were removed from its container), "
                f"while `HorizontalPodAutoscaler/{self.HPA_NAME}` scales that deployment on CPU utilization "
                f"(target {self.HPA_CPU_TARGET_PERCENT}%, {self.HPA_MIN_REPLICAS}-{self.HPA_MAX_REPLICAS} replicas). "
                "Utilization is computed relative to the CPU request, so the HPA reports `<unknown>/60%`, "
                "`ScalingActive=False` and `FailedGetResourceMetric` (`missing request for cpu`) and can never scale "
                f"`{faulty_service}` out under load. The pods themselves remain Running/Ready. A valid mitigation "
                "restores a computable CPU metric, typically by restoring the CPU request; manually scaling the "
                "deployment without fixing the HPA is not sufficient."
            ),
            oracle_factory=lambda problem: HPAControlPlaneMitigationOracle(
                problem=problem, deployment_name=problem.faulty_service, hpa_name=problem.HPA_NAME
            ),
        )

    def _remove_effective_cpu_request(self, service: str):
        """Remove requests.cpu and limits.cpu with a JSON patch.

        The original re-applies an edited manifest without its last-applied
        annotation, which (on Helm-managed Deployments) merges and keeps the
        CPU fields.
        """
        deployment = self._get_deployment_json(service)
        ops = []
        for i, container in enumerate(deployment["spec"]["template"]["spec"]["containers"]):
            resources = container.get("resources") or {}
            for kind in ("requests", "limits"):
                if "cpu" in (resources.get(kind) or {}):
                    ops.append({"op": "remove", "path": f"/spec/template/spec/containers/{i}/resources/{kind}/cpu"})
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        annotations = deployment["spec"]["template"].get("metadata", {}).get("annotations")
        if annotations is None:
            ops.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": {}})
        ops.append(
            {
                "op": "add",
                "path": "/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt",
                "value": stamp,
            }
        )
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {service} -n {self.namespace} --type=json -p '{json.dumps(ops)}'"
        )
        print(f"Removed effective CPU request from Deployment/{service}")
