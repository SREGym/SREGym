"""SREGym-Lite configuration faults re-targeted at the Incident Arena apps.

These faults edit a single workload's container configuration (environment,
command, scheduling). The ports keep the original mechanism and adapt the
names, values and probes that were specific to Astronomy Shop.
"""

from __future__ import annotations

import json
import textwrap

from sregym.conductor.oracles.assign_non_existent_node_mitigation import AssignNonExistentNodeMitigationOracle
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.edge_request_filter_mitigation import EdgeRequestFilterMitigationOracle
from sregym.conductor.oracles.env_variable_shadowing_mitigation import EnvVariableShadowingMitigationOracle
from sregym.conductor.oracles.incorrect_port import IncorrectPortAssignmentMitigationOracle
from sregym.conductor.problems.edge_request_filter_cpu_saturation import EdgeRequestFilterCPUSaturation
from sregym.conductor.problems.env_variable_shadowing import EnvVariableShadowing
from sregym.conductor.problems.incorrect_port_assignment import IncorrectPortAssignment
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected


class EnvVariableShadowingIA(EnvVariableShadowing):
    """A second ``DATABASE_URL`` definition points Saleor's API at localhost."""

    def __init__(
        self,
        app_name: str = "saleor",
        faulty_service: str = "saleor-api",
        container_name: str = "api",
        service_name: str = "svc-saleor-api",
        env_name: str = "DATABASE_URL",
        expected_value: str = "postgres://saleor_app:agentrepair-app@postgres:5432/saleor",
        shadow_value: str = "postgres://saleor_app:agentrepair-app@localhost:5432/saleor",
        health_path: str = "/health/",
        expected_content: str = "",
    ):
        self.faulty_service = faulty_service
        self.container_name = container_name
        self.service_name = service_name
        self.ENV_NAME = env_name
        self.EXPECTED_VALUE = expected_value
        self.SHADOW_VALUE = shadow_value
        self.health_path = health_path
        self.expected_content = expected_content
        self._baseline_template = None
        ported(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"Container `{container_name}` of deployment `{faulty_service}` defines `{env_name}` twice. The "
                f"later definition (`{shadow_value}`) shadows the intended one (`{expected_value}`), so the API "
                "connects to a database on localhost that does not exist instead of the `postgres` Service, and "
                "its requests fail although the earlier, correct definition still appears in the pod spec."
            ),
            oracle_factory=EnvVariableShadowingMitigationOracle,
        )

    def _target_container_and_env(self, deployment):
        for container_index, container in enumerate(deployment.spec.template.spec.containers):
            if container.name == self.container_name:
                return container_index, container.env or []
        raise RuntimeError(f"Container '{self.container_name}' was not found")

    def _wait_for_rollout(self):
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )


class EdgeRequestFilterIA(EdgeRequestFilterCPUSaturation):
    """A vulnerable WAF regex in Frappe's nginx edge (``erp-nginx``, Service ``erp``).

    The filter runs as the original Perl edge request filter (the Frappe image
    ships Perl) in front of gunicorn. Crafted requests come from inside the
    chart's load generator pod, as they came from Astronomy Shop's.
    """

    def __init__(
        self,
        app_name: str = "frappe",
        faulty_service: str = "erp-nginx",
        container_name: str = "nginx",
        service_name: str = "erp",
        listen_port: int = 8080,
        upstream: tuple[str, int] = ("erp-gunicorn", 8000),
        site_host: str = "svc-frappe-web",
        health_path: str = "/api/method/ping",
    ):
        self.faulty_service = faulty_service
        self.container_name = container_name
        self.service_name = service_name
        self.listen_port = listen_port
        self.upstream = upstream
        self.site_host = site_host
        self.health_path = health_path
        self.traffic_source = "loadgen"
        self.regex_env = "WAF_RULE_REGEX"
        self.bad_regex = "^([a-zA-Z]+)*$"
        self.safe_regex = "^[A-Za-z]+$"
        # Frappe's nginx rejects a 5000-character URI; 4000 still backtracks
        # catastrophically against the vulnerable rule.
        self.crafted_payload = "a" * 4000 + "!"
        self.probe_host_header = site_host
        self.cpu_limit = "100m"
        self.memory_limit = "65Mi"
        self.process_marker = "edge-traffic-replay"
        self.driver_interval_seconds = 0.05
        self.driver_log = "/tmp/edge-traffic-replay.log"
        self.driver_pid = "/tmp/edge-traffic-replay.pid"
        self._baseline_template = None
        ported(
            self,
            app_name,
            component=faulty_service,
            description=(
                f"A recent edge/WAF request-filter rule update on deployment `{faulty_service}` (the site's edge "
                f"in front of gunicorn) introduced the vulnerable regex `{self.bad_regex}`. Requests carrying long "
                "near-matching `waf` query values trigger catastrophic backtracking in the edge request filter, "
                "saturating the edge's CPU limit and timing out otherwise healthy HTTP paths through it. The fix "
                f"is to roll back or disable the bad rule, or replace it with a linear-time equivalent such as "
                f"`{self.safe_regex}`."
            ),
            oracle_factory=EdgeRequestFilterMitigationOracle,
        )

    def _edge_filter_script(self) -> str:
        script = super()._edge_filter_script()
        host, port = self.upstream
        # Same filter; Frappe's listen port and upstream, and the Host header
        # that selects the Frappe site.
        script = script.replace("$ENV{ENVOY_PORT} || 8080", f"$ENV{{EDGE_LISTEN_PORT}} || {self.listen_port}")
        script = script.replace('$ENV{FRONTEND_HOST} || "frontend"', f'$ENV{{UPSTREAM_HOST}} || "{host}"')
        script = script.replace("$ENV{FRONTEND_PORT} || 8080", f"$ENV{{UPSTREAM_PORT}} || {port}")
        script = script.replace(
            '. $upstream_host . ":" . $upstream_port', f'. ($ENV{{SITE_HOST}} || "{self.site_host}")'
        )
        return script

    def _patch_edge_proxy(self):
        script = self._edge_filter_script()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "edge.platform/change-ticket": "EDGE-1847",
                            "edge.platform/filter-profile": "managed-rules-v2",
                        }
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": self.container_name,
                                "command": ["/usr/bin/perl", "-e"],
                                "args": [script],
                                "env": [
                                    {"name": self.regex_env, "value": self.bad_regex},
                                    {"name": "WAF_RULE_ENABLED", "value": "true"},
                                ],
                                "resources": {"limits": {"cpu": self.cpu_limit, "memory": self.memory_limit}},
                            }
                        ]
                    },
                }
            }
        }
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            name=self.faulty_service, namespace=self.namespace, body=body
        )
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s"
        )

    def _python(self, script: str) -> str:
        """Run ``script`` with whichever Python the traffic source image ships."""
        launcher = f"exec({script!r})"
        return f'PY=$(command -v python3 || command -v python); exec "$PY" -c {json.dumps(launcher)}'

    def _start_crafted_traffic(self):
        driver = textwrap.dedent(
            f"""
            import time
            import urllib.request

            marker = "{self.process_marker}"
            url = "http://{self.service_name}:{self.listen_port}/?waf={self.crafted_payload}"
            while True:
                try:
                    urllib.request.urlopen(url, timeout=10).read()
                except Exception:
                    pass
                time.sleep({self.driver_interval_seconds})
            """
        ).strip()
        launcher = f"exec({driver!r})"
        shell = (
            f"rm -f {self.driver_log} {self.driver_pid}; PY=$(command -v python3 || command -v python); "
            f'nohup "$PY" -c {json.dumps(launcher)} >{self.driver_log} 2>&1 & echo $! > {self.driver_pid}'
        )
        self.kubectl.exec_command(
            f"kubectl exec deployment/{self.traffic_source} -n {self.namespace} -c {self.traffic_source} "
            f"-- /bin/sh -c {json.dumps(shell)}"
        )

    def _stop_crafted_traffic(self):
        marker_split = max(1, len(self.process_marker) // 2)
        cleanup = textwrap.dedent(
            f"""
            import os
            import signal

            marker = {self.process_marker[:marker_split]!r} + {self.process_marker[marker_split:]!r}
            for entry in os.listdir("/proc"):
                if not entry.isdigit() or int(entry) == os.getpid():
                    continue
                try:
                    with open(f"/proc/{{entry}}/cmdline", "rb") as command_file:
                        if marker.encode() in command_file.read():
                            os.kill(int(entry), signal.SIGTERM)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    pass
            for path in ({self.driver_pid!r}, {self.driver_log!r}):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            """
        ).strip()
        self.kubectl.exec_command(
            f"kubectl exec deployment/{self.traffic_source} -n {self.namespace} -c {self.traffic_source} "
            f"-- /bin/sh -c {json.dumps(self._python(cleanup))}"
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._capture_baseline_template()
        self._patch_edge_proxy()
        self._start_crafted_traffic()
        print(f"Fault: EdgeRequestFilter | Deployment: {self.faulty_service} | Namespace: {self.namespace}\n")


class IncorrectPortAssignmentIA(IncorrectPortAssignment):
    """Frappe's nginx edge points at gunicorn's wrong port and is pinned to a missing node."""

    def __init__(
        self,
        app_name: str = "frappe",
        faulty_service: str = "erp-nginx",
        env_var: str = "BACKEND",
        correct_port: str = "8000",
        incorrect_port: str = "8001",
    ):
        self.faulty_service = faulty_service
        self.env_var = env_var
        self.correct_port = correct_port
        self.incorrect_port = incorrect_port
        self.unscheduable = True
        # The dependency is gunicorn's HTTP port, not Astronomy Shop's gRPC catalog.
        self.tcp_dependency_probe = True
        ported(
            self,
            app_name,
            component=f"deployment/{faulty_service}",
            description=(
                f"Two faults are active at the same time on deployment `{faulty_service}`: (1) its `{env_var}` "
                f"environment variable points to the wrong backend port ({incorrect_port} instead of "
                f"{correct_port}), so nginx cannot reach gunicorn, and (2) the deployment is pinned to a "
                "non-existent node via nodeSelector (`kubernetes.io/hostname: extra-node`), keeping its pods "
                "Pending. Symptoms are unschedulable pod events and, once scheduled, connection-refused errors "
                "from the bad upstream port."
            ),
            oracle_factory=lambda problem: CompoundedOracle(
                problem,
                IncorrectPortAssignmentMitigationOracle(problem=problem, require_source_ready=False),
                AssignNonExistentNodeMitigationOracle(problem=problem),
            ),
        )
        self.injector = ApplicationFaultInjector(namespace=self.namespace)
        self.injectors = {
            "incorrect_port_assignment": self.injector,
            "assign_to_non_existent_node": VirtualizationFaultInjector(namespace=self.namespace),
        }

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injectors["assign_to_non_existent_node"]._recover(
            fault_type="assign_to_non_existent_node", microservices=[self.faulty_service]
        )
        self.injector.recover_incorrect_port_assignment(
            deployment_name=self.faulty_service, env_var=self.env_var, correct_port=self.correct_port
        )
