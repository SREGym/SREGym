"""Slack Spine: service configuration."""

from __future__ import annotations

import json
from pathlib import Path

from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.oracles.missing_env_variable_mitigation import MissingEnvVariableMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.oracles.scale_pod_zero_mitigation import ScalePodZeroMitigationOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.conductor.oracles.target_port_mitigation import TargetPortMisconfigMitigationOracle
from sregym.conductor.problems.apps.slack_spine.manifests import (
    APP,
    _apply,
    _clean_manifest,
    _get,
    _replace_pods,
    _rollout_status,
    _with_port,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import (
    configure_port,
    make_app,
    reopen_connections,
)
from sregym.conductor.problems.missing_env_variable import MissingEnvVariable
from sregym.conductor.problems.scale_pod import ScalePodSocialNet
from sregym.conductor.problems.service_port_conflict import ServicePortConflict
from sregym.conductor.problems.wrong_service_selector import WrongServiceSelector
from sregym.utils.decorators import mark_fault_injected


class IncorrectPortAssignmentSlack(Problem):
    """svc-workspace's REDIS_URL points at the wrong Redis port.

    svc-workspace serves org settings from a Redis cache; svc-channel
    revalidates the org policy on svc-workspace for every message send, so
    sends fail with 503 ``authz_unavailable``. The role touches Redis only
    when serving (its startup and health endpoint do not), so the
    misconfigured pod is Ready and replaces the healthy one, as Astronomy
    Shop's checkout did. (svc-auth was not used: it loads its signing keys
    from Redis at startup, so its misconfigured pod never becomes Ready.)
    """

    def __init__(
        self,
        faulty_service: str = "svc-workspace",
        env_var: str = "REDIS_URL",
        correct_port: str = "6379",
        incorrect_port: str = "6380",
    ):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        self.env_var = env_var
        self.correct_port = correct_port
        self.incorrect_port = incorrect_port
        self.tcp_dependency_probe = True
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The `{env_var}` environment variable of deployment `{faulty_service}` points to the wrong Redis "
                f"port (`redis://redis:{incorrect_port}` instead of `redis://redis:{correct_port}`). Nothing listens "
                "there, so svc-workspace cannot reach its org-settings cache and `GET /orgs/:id/settings` fails. "
                "svc-channel revalidates the org policy on svc-workspace for every message send, so message posts "
                "fail with 503 `authz_unavailable` while the svc-workspace pod stays Running and Ready."
            ),
            oracle_factory=lambda problem: IncorrectPortAssignmentMitigationOracle(problem, address_is_url=True),
        )

    def _set_port(self, port: str) -> None:
        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        container = deployment.spec.template.spec.containers[0]
        env = [
            {"name": var.name, "value": _with_port(var.value, port)}
            for var in container.env or []
            if var.name == self.env_var
        ]
        if not env:
            raise ValueError(f"Environment variable '{self.env_var}' not found in '{self.faulty_service}'")
        patch = {"spec": {"template": {"spec": {"containers": [{"name": container.name, "env": env}]}}}}
        self.kubectl.patch_deployment(self.faulty_service, self.namespace, patch)
        _rollout_status(self, self.faulty_service)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._set_port(self.incorrect_port)
        print(f"{self.faulty_service}: {self.env_var} -> port {self.incorrect_port}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._set_port(self.correct_port)


class MissingEnvVariableSlack(MissingEnvVariable):
    """REDIS_URL is removed from svc-notification, which refuses to start without it."""

    def __init__(self, faulty_service: str = "svc-notification", env_var: str = "REDIS_URL"):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        self.app_name = APP
        self.env_var = env_var
        self.env_var_value = "redis://redis:6379"
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The required environment variable `{env_var}` (`{self.env_var_value}`) was removed from deployment "
                f"`{faulty_service}`. The notification role keeps unread counters in Redis and refuses to start "
                "without it (`servicekit: role 'notification' requires REDIS_URL but it is not set`), so its pod "
                "crash-loops, the Service has no endpoints, and users' unread-count requests (and svc-message's "
                "notification fan-out) fail."
            ),
            oracle_factory=MissingEnvVariableMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # The new pod never becomes Ready: replace the old one so it cannot mask the fault.
        _replace_pods(self, self.faulty_service)


class ScalePodZeroSlack(ScalePodSocialNet):
    def __init__(self, faulty_service: str = "svc-notification"):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The deployment `{faulty_service}` is scaled to zero replicas, removing every pod of Slack Spine's "
                "notification role. Its Service has no endpoints, so users' unread-count requests (`GET /unread`) "
                "and svc-message's notification fan-out fail immediately."
            ),
            oracle_factory=ScalePodZeroMitigationOracle,
        )


class K8STargetPortMisconfigSlack(Problem):
    def __init__(self, faulty_service: str = "svc-workspace", client_deployment: str = "svc-channel"):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        self.client_deployment = client_deployment
        self.from_port = "http"
        self.to_port = 9999
        configure_port(
            self,
            APP,
            component=f"service/{faulty_service}",
            description=(
                f"The Service `{faulty_service}` targetPort was changed from the container port `http` (8000) to "
                "`9999`, where no process listens. Requests reach the pods' IP on a closed port and fail with "
                "connection refused; svc-channel revalidates the org policy on svc-workspace for every message send, "
                "so sends fail with 503 `authz_unavailable` while every pod stays Running and Ready."
            ),
            oracle_factory=lambda problem: TargetPortMisconfigMitigationOracle(
                problem, valid_target_ports=("http", 8000)
            ),
        )

    def _set_target_port(self, value) -> None:
        ports = self.kubectl.get_service_json(self.faulty_service, self.namespace)["spec"]["ports"]
        ports[0]["targetPort"] = value
        patch = json.dumps([{"op": "replace", "path": "/spec/ports", "value": ports}])
        self.kubectl.exec_command_checked(
            f"kubectl patch service {self.faulty_service} -n {self.namespace} --type=json -p '{patch}'"
        )

    @mark_fault_injected
    def inject_fault(self):
        self._set_target_port(self.to_port)
        print(f"[FAULT INJECTED] {self.faulty_service} targetPort {self.from_port} -> {self.to_port}")
        # Kept-alive connections to the old target port would otherwise keep working.
        reopen_connections(self, self.client_deployment)

    @mark_fault_injected
    def recover_fault(self):
        self._set_target_port(self.from_port)
        print(f"[FAULT RECOVERED] {self.faulty_service}")


class ServicePortConflictSlack(ServicePortConflict):
    def __init__(self, faulty_service: str = "svc-workspace"):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        self.app_name = APP
        self.conflicting_port = 9100
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"The pod template of deployment `{faulty_service}` binds hostPort {self.conflicting_port}, which "
                "collides with the prometheus-node-exporter DaemonSet (observe namespace) that already occupies port "
                f"{self.conflicting_port} on every node. The new pod cannot be scheduled (FailedScheduling: didn't "
                "have free ports), the deployment has no available replica, and message sends fail because "
                "svc-channel revalidates the org policy on svc-workspace for every send."
            ),
            oracle_factory=MitigationOracle,
        )

    def _assert_port_taken(self) -> None:
        """The conflict needs a host-port holder (node-exporter) on every schedulable node."""
        holders = {
            pod.spec.node_name
            for pod in self.kubectl.core_v1_api.list_pod_for_all_namespaces().items
            if pod.spec.node_name
            and any(
                (port.host_port or (port.container_port if pod.spec.host_network else None)) == self.conflicting_port
                for container in pod.spec.containers
                for port in container.ports or []
            )
        }
        schedulable = {
            node.metadata.name
            for node in self.kubectl.core_v1_api.list_node().items
            if not any(taint.effect == "NoSchedule" for taint in node.spec.taints or [])
        }
        if not schedulable <= holders:
            raise RuntimeError(
                f"hostPort {self.conflicting_port} is free on {sorted(schedulable - holders)}: "
                "prometheus-node-exporter must run on every schedulable node for this fault"
            )

    @mark_fault_injected
    def inject_fault(self):
        self._assert_port_taken()
        super().inject_fault()


class MissingServiceSlack(Problem):
    """The Service `svc-channel` is deleted; svc-message's per-send authz check can no longer resolve it."""

    def __init__(self, faulty_service: str = "svc-channel", client_deployment: str = "svc-message"):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        self.client_deployment = client_deployment
        self.app_name = APP
        self.expected_service_port = 8000
        configure_port(
            self,
            APP,
            component=f"service/{faulty_service}",
            description=(
                f"The Kubernetes Service `{faulty_service}` has been deleted, so its name no longer resolves through "
                "cluster DNS although the svc-channel pods are still Running and Ready. svc-message resolves channel "
                "authorization on svc-channel for every message send, so sends fail with 503 `authz_unavailable`."
            ),
            # Every pod stays Ready (health endpoints do not call svc-channel),
            # so the generic check alone cannot see the fault: the Service must
            # exist again with reachable endpoints.
            oracle_factory=lambda problem: CompoundedOracle(
                problem, MitigationOracle(problem), ServiceEndpointMitigationOracle(problem)
            ),
        )

    @property
    def _backup_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-{self.faulty_service}-service.json")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._backup_path.write_text(json.dumps(_clean_manifest(_get(self, "service", self.faulty_service))))
        self.kubectl.exec_command_checked(f"kubectl delete service {self.faulty_service} -n {self.namespace}")
        print(f"Deleted service {self.faulty_service}")
        # Established keep-alive connections survive the deletion: make the caller re-resolve.
        reopen_connections(self, self.client_deployment)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        _apply(self, json.loads(self._backup_path.read_text()))
        print(f"Recreated service {self.faulty_service}")


class MissingConfigMapSlack(Problem):
    """The shared `app-config` ConfigMap is deleted and svc-message restarted (scale 0 -> 1, as the original)."""

    def __init__(self, faulty_service: str = "svc-message", configmap: str = "app-config"):
        super().__init__(app=make_app(APP))
        self.faulty_service = faulty_service
        self.configmap = configmap
        self.app_name = APP
        configure_port(
            self,
            APP,
            component=faulty_service,
            description=(
                f"The ConfigMap `{configmap}` (the `app.yaml` role configuration that every svc-* deployment mounts "
                f"at /config) has been deleted. Pods still running keep their mounted copy, but deployment "
                f"`{faulty_service}` was restarted: its new pod cannot mount the volume (FailedMount: configmap "
                f'"{configmap}" not found) and stays in ContainerCreating, so the message role is down and users\' '
                "history reads and message sends fail."
            ),
            oracle_factory=MitigationOracle,
        )

    @property
    def _backup_path(self) -> Path:
        return Path(f"/tmp/sregym-{self.namespace}-{self.configmap}-configmap.json")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._backup_path.write_text(json.dumps(_clean_manifest(_get(self, "configmap", self.configmap))))
        self.kubectl.exec_command_checked(f"kubectl delete configmap {self.configmap} -n {self.namespace}")
        print(f"Deleted ConfigMap {self.configmap}")
        _replace_pods(self, self.faulty_service)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        _apply(self, json.loads(self._backup_path.read_text()))
        self.kubectl.exec_command(f"kubectl rollout restart deployment {self.faulty_service} -n {self.namespace}")
        _rollout_status(self, self.faulty_service)


class WrongServiceSelectorIA(WrongServiceSelector):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-channel", port: int = 8000):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.expected_service_port = port
        configure_port(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"The Service `{faulty_service}` has a misconfigured selector with an extra label "
                f"(`current_service_name: {faulty_service}`) that no pod carries, so it no longer matches its "
                "backing pods. The Service has zero endpoints despite a healthy Deployment, and calls routed "
                "through it fail with connection errors (svc-message's channel authorization check, so message "
                "sends fail)."
            ),
            oracle_factory=ServiceEndpointMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # Kept-alive connections to the old pods would otherwise keep working.
        reopen_connections(self, self.faulty_service)
