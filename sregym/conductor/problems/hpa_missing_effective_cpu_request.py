"""Problem: HPA cannot compute CPU utilization due to missing effective CPU requests.

This problem models a Kubernetes HorizontalPodAutoscaler control-loop failure
where the Hotel Reservation frontend pods remain Running/Ready, but the HPA
cannot compute CPU utilization because the target pods lack an effective CPU
request.
"""

import json
import time
from pathlib import Path

import yaml

from sregym.conductor.oracles.hpa_control_plane_mitigation import HPAControlPlaneMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class HPAMissingEffectiveCPURequest(Problem):
    """Inject a broken CPU-utilization HPA on Hotel Reservation frontend."""

    HPA_NAME = "frontend-capacity"
    HPA_CPU_TARGET_PERCENT = 60
    HPA_MIN_REPLICAS = 1
    HPA_MAX_REPLICAS = 5
    HPA_DEFAULT_CPU_REQUEST = "100m"
    HPA_DEFAULT_MEMORY_REQUEST = "128Mi"
    HPA_DEFAULT_CPU_LIMIT = "1"
    HPA_SNAPSHOT_SUFFIX = "hpa_effective_cpu_request_original"
    ROLLOUT_RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

    def __init__(self):
        self.faulty_service = "frontend"

        super().__init__(app=HotelReservation())

        self.kubectl = KubeCtl()

        self.root_cause = self.build_structured_root_cause(
            component=(
                f"Deployment/{self.faulty_service} resource configuration and HorizontalPodAutoscaler/{self.HPA_NAME}"
            ),
            namespace=self.namespace,
            description=(
                f"The frontend autoscaling control loop is broken because `Deployment/{self.faulty_service}` "
                f"produces pods without an effective CPU request, while `HorizontalPodAutoscaler/{self.HPA_NAME}` "
                "is configured to scale that deployment using CPU utilization. CPU utilization is calculated "
                "relative to CPU requests, so the HPA cannot compute the metric and reports `<unknown>/60%`, "
                "`ScalingActive=False`, and `FailedGetResourceMetric` with a message like "
                "`missing request for cpu`. The frontend pods may still be Running/Ready; the fault is the "
                "missing effective CPU request on the frontend workload causing the HPA control loop to fail. "
                "A complete diagnosis should identify both the missing effective CPU request and the resulting "
                "HPA CPU-utilization metric failure. Merely reporting that pods are healthy, or merely noting "
                "a missing CPU request without tying it to HPA/autoscaling behavior, is incomplete. "
                "A valid mitigation restores a computable CPU metric, typically by restoring CPU requests. "
                "Manually scaling the deployment without fixing the HPA is not sufficient."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        self.mitigation_oracle = HPAControlPlaneMitigationOracle(
            problem=self,
            deployment_name=self.faulty_service,
            hpa_name=self.HPA_NAME,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._ensure_no_cpu_defaulting_limit_range()
        # Verify metrics-server is producing data first so a pre-existing outage isn't blamed on the fault.
        try:
            self._wait_for_target_pod_metrics(self.faulty_service)
        except RuntimeError as exc:
            self._ensure_metrics_api_available()
            raise RuntimeError(
                f"metrics-server did not produce pod metrics for Deployment/{self.faulty_service}. "
                "The metrics API is available, but kubectl top did not return target pod "
                f"metrics in time. Original error: {exc}"
            ) from exc
        self._save_deployment_snapshot(self.faulty_service)
        self._remove_effective_cpu_request(self.faulty_service)
        self._wait_for_rollout(self.faulty_service)
        self._delete_hpa_if_exists(self.HPA_NAME)
        self._create_cpu_utilization_hpa(self.faulty_service, self.HPA_NAME)
        self._wait_for_hpa_missing_cpu_request(self.HPA_NAME)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        snapshot_path = self._hpa_snapshot_path(self.faulty_service)
        restored_from_snapshot = False

        if Path(snapshot_path).exists():
            apply_out = self.kubectl.exec_command(f"kubectl apply -f {snapshot_path} -n {self.namespace}")
            print(f"Restored Deployment/{self.faulty_service} from snapshot: {apply_out.strip()}")
            restored_from_snapshot = True
        else:
            print(
                f"No HPA CPU request snapshot found for Deployment/{self.faulty_service}; "
                "falling back to kubectl set resources"
            )
            self.kubectl.exec_command(
                f"kubectl set resources deployment/{self.faulty_service} -n {self.namespace} "
                f"--requests=cpu={self.HPA_DEFAULT_CPU_REQUEST},memory={self.HPA_DEFAULT_MEMORY_REQUEST} "
                f"--limits=cpu={self.HPA_DEFAULT_CPU_LIMIT}"
            )

        # Snapshot apply already restores the pod template; force a rollout only in the fallback path.
        if not restored_from_snapshot:
            self._force_hpa_rollout(self.faulty_service)

        self._wait_for_rollout(self.faulty_service)
        self._ensure_hpa_exists(self.faulty_service, self.HPA_NAME)
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def _ensure_metrics_api_available(self):
        raw = self.kubectl.exec_command("kubectl get apiservice v1beta1.metrics.k8s.io -o json")
        api_service = self._parse_json_or_raise(raw, "metrics.k8s.io APIService")

        conditions = api_service.get("status", {}).get("conditions", [])
        for condition in conditions:
            if condition.get("type") == "Available" and condition.get("status") == "True":
                return

        raise RuntimeError(
            "metrics.k8s.io is required for this HPA problem, but "
            "v1beta1.metrics.k8s.io is not Available=True. Repair metrics-server before running it."
        )

    def _ensure_no_cpu_defaulting_limit_range(self):
        raw = self.kubectl.exec_command(f"kubectl get limitrange -n {self.namespace} -o json")
        limit_ranges = self._parse_json_or_raise(raw, "LimitRange list")

        for limit_range in limit_ranges.get("items", []):
            for item in limit_range.get("spec", {}).get("limits", []):
                default_request = item.get("defaultRequest") or {}
                default_limit = item.get("default") or {}

                if "cpu" in default_request or "cpu" in default_limit:
                    name = limit_range.get("metadata", {}).get("name", "<unknown>")
                    raise RuntimeError(
                        f"LimitRange/{name} in namespace {self.namespace} sets a default CPU "
                        "request or CPU limit. That would reintroduce an effective CPU request "
                        "and confound this HPA fault."
                    )

    def _wait_for_target_pod_metrics(self, service: str, timeout: int = 150):
        selector = self._deployment_selector(service)
        deadline = time.monotonic() + timeout
        last_output = ""

        while time.monotonic() < deadline:
            output = self.kubectl.exec_command(
                f"kubectl top pods -n {self.namespace} -l '{selector}' --no-headers"
            ).strip()
            last_output = output

            if self._top_output_has_metric_line(output):
                return

            time.sleep(5)

        raise RuntimeError(
            f"metrics-server did not report pod metrics for Deployment/{service} within {timeout}s. "
            f"Last output: {last_output!r}"
        )

    @staticmethod
    def _top_output_has_metric_line(output: str) -> bool:
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            lowered = stripped.lower()
            if lowered.startswith("error") or "metrics api not available" in lowered or "no resources found" in lowered:
                continue

            columns = stripped.split()
            if len(columns) >= 3:
                return True

        return False

    def _save_deployment_snapshot(self, service: str):
        deployment = self._get_sanitized_deployment(service)
        snapshot_path = self._hpa_snapshot_path(service)

        with open(snapshot_path, "w") as file:
            yaml.safe_dump(deployment, file)

        print(f"Saved pre-injection Deployment/{service} snapshot to {snapshot_path}")

    def _remove_effective_cpu_request(self, service: str):
        deployment = self._get_sanitized_deployment(service)
        containers = deployment["spec"]["template"]["spec"]["containers"]

        for container in containers:
            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}

            requests.pop("cpu", None)
            limits.pop("cpu", None)

            if requests:
                resources["requests"] = requests
            else:
                resources.pop("requests", None)

            if limits:
                resources["limits"] = limits
            else:
                resources.pop("limits", None)

            if resources:
                container["resources"] = resources
            else:
                container.pop("resources", None)

        self._annotate_deployment_for_hpa_rollout(deployment)

        faulty_path = f"/tmp/{service}_hpa_missing_effective_cpu_request_faulty.yaml"
        with open(faulty_path, "w") as file:
            yaml.safe_dump(deployment, file)

        # limits.cpu is removed alongside requests.cpu because K8s uses a CPU limit as the
        # effective request when the request is absent, which would prevent the fault from taking hold.
        apply_out = self.kubectl.exec_command(f"kubectl apply -f {faulty_path} -n {self.namespace}")
        print(f"Removed effective CPU request from Deployment/{service}: {apply_out.strip()}")

    def _ensure_hpa_exists(self, service: str, hpa_name: str):
        existing = self.kubectl.exec_command(
            f"kubectl get hpa {hpa_name} -n {self.namespace} --ignore-not-found -o name"
        ).strip()

        if not existing:
            self._create_cpu_utilization_hpa(service, hpa_name)

    def _create_cpu_utilization_hpa(self, service: str, hpa_name: str):
        hpa = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": hpa_name,
                "namespace": self.namespace,
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": service,
                },
                "minReplicas": self.HPA_MIN_REPLICAS,
                "maxReplicas": self.HPA_MAX_REPLICAS,
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": self.HPA_CPU_TARGET_PERCENT,
                            },
                        },
                    }
                ],
            },
        }

        hpa_path = f"/tmp/{hpa_name}.yaml"
        with open(hpa_path, "w") as file:
            yaml.safe_dump(hpa, file)

        apply_out = self.kubectl.exec_command(f"kubectl apply -f {hpa_path} -n {self.namespace}")
        print(f"Created or updated HPA/{hpa_name}: {apply_out.strip()}")

    def _delete_hpa_if_exists(self, hpa_name: str):
        self.kubectl.exec_command(f"kubectl delete hpa {hpa_name} -n {self.namespace} --ignore-not-found")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            existing = self.kubectl.exec_command(
                f"kubectl get hpa {hpa_name} -n {self.namespace} --ignore-not-found -o name"
            ).strip()

            if not existing:
                return

            time.sleep(1)

        raise RuntimeError(f"Timed out waiting for old HPA/{hpa_name} to be deleted")

    def _wait_for_hpa_missing_cpu_request(self, hpa_name: str, timeout: int = 120):
        deadline = time.monotonic() + timeout
        last_status = ""

        while time.monotonic() < deadline:
            raw = self.kubectl.exec_command(f"kubectl get hpa {hpa_name} -n {self.namespace} -o json")

            try:
                hpa = self._parse_json_or_raise(raw, f"HPA/{hpa_name}")
            except RuntimeError as exc:
                last_status = str(exc)
                time.sleep(5)
                continue

            if self._hpa_reports_missing_cpu_request(hpa):
                print(f"HPA/{hpa_name} reports FailedGetResourceMetric due to missing CPU request")
                return

            last_status = self._summarize_hpa_status(hpa)
            time.sleep(5)

        raise RuntimeError(
            f"HPA/{hpa_name} did not report the expected missing CPU request failure "
            f"within {timeout}s. Last status: {last_status}"
        )

    @staticmethod
    def _hpa_reports_missing_cpu_request(hpa: dict) -> bool:
        for condition in hpa.get("status", {}).get("conditions", []):
            if condition.get("type") != "ScalingActive":
                continue

            status_is_false = condition.get("status") == "False"
            failed_resource_metric = condition.get("reason") == "FailedGetResourceMetric"
            message = condition.get("message", "").lower()

            if status_is_false and failed_resource_metric and "missing request for cpu" in message:
                return True

        return False

    @staticmethod
    def _summarize_hpa_status(hpa: dict) -> str:
        conditions = []
        for condition in hpa.get("status", {}).get("conditions", []):
            conditions.append(
                f"{condition.get('type')}={condition.get('status')}/"
                f"{condition.get('reason')}: {condition.get('message', '')}"
            )
        return "; ".join(conditions) or "no HPA conditions yet"

    def _force_hpa_rollout(self, service: str):
        restart_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            self.ROLLOUT_RESTART_ANNOTATION: restart_stamp,
                        }
                    }
                }
            }
        }
        patch_json = json.dumps(patch)
        self.kubectl.exec_command(
            f"kubectl patch deployment {service} -n {self.namespace} --type=merge -p '{patch_json}'"
        )

    def _wait_for_rollout(self, service: str, timeout: int = 300):
        output = self.kubectl.exec_command(
            f"kubectl rollout status deployment/{service} -n {self.namespace} --timeout={timeout}s"
        )
        lowered = output.lower()

        if "error" in lowered or "timed out" in lowered:
            raise RuntimeError(f"Deployment/{service} rollout did not complete: {output}")

    def _deployment_selector(self, service: str) -> str:
        deployment = self._get_deployment_json(service)
        labels = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if not labels:
            raise RuntimeError(f"Deployment/{service} does not have matchLabels selector")

        return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))

    def _get_deployment_json(self, service: str) -> dict:
        raw = self.kubectl.exec_command(f"kubectl get deployment {service} -n {self.namespace} -o json")
        return self._parse_json_or_raise(raw, f"Deployment/{service}")

    def _get_sanitized_deployment(self, service: str) -> dict:
        deployment = self._get_deployment_json(service)

        deployment.pop("status", None)
        metadata = deployment.setdefault("metadata", {})
        for field in ("creationTimestamp", "generation", "managedFields", "resourceVersion", "uid"):
            metadata.pop(field, None)

        annotations = metadata.get("annotations") or {}
        annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
        if annotations:
            metadata["annotations"] = annotations
        else:
            metadata.pop("annotations", None)

        return deployment

    def _annotate_deployment_for_hpa_rollout(self, deployment: dict):
        restart_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        template_metadata = deployment["spec"]["template"].setdefault("metadata", {})
        annotations = template_metadata.setdefault("annotations", {})
        annotations[self.ROLLOUT_RESTART_ANNOTATION] = restart_stamp

    def _hpa_snapshot_path(self, service: str) -> str:
        return f"/tmp/{service}_{self.HPA_SNAPSHOT_SUFFIX}.yaml"

    @staticmethod
    def _parse_json_or_raise(raw: str, resource_name: str) -> dict:
        stripped = raw.strip()

        if not stripped:
            raise RuntimeError(f"kubectl returned no output for {resource_name}")

        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse kubectl JSON for {resource_name}: {exc}; output={stripped[:500]!r}"
            ) from exc
