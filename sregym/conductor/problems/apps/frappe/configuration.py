"""Frappe: configuration."""

from __future__ import annotations

import json
import shlex

from kubernetes import client

from sregym.conductor.oracles.apps.frappe.state import FrappeFaultStateOracle
from sregym.conductor.oracles.deployment_readiness import DeploymentReadinessOracle
from sregym.conductor.oracles.edge_request_filter_mitigation import EdgeRequestFilterMitigationOracle
from sregym.conductor.problems.apps.frappe.helpers import (
    APP,
    GUNICORN,
    WEB_SERVICE,
    apply_manifest,
    replace_pods_with_patch,
    rollout_status,
    selector_of,
    state_file,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.edge_request_filter_cpu_saturation import EdgeRequestFilterCPUSaturation
from sregym.conductor.problems.environment import configure_port, make_app
from sregym.conductor.problems.psa_restricted_blocks_recreation import (
    PSA_ENFORCE_LABEL,
    PSA_ENFORCE_VERSION_LABEL,
    RESTRICTED_PROFILE,
    PSARestrictedBlocksRecreation,
)
from sregym.service.apps.frappe.application import Frappe
from sregym.utils.decorators import mark_fault_injected


class PSARestrictedBlocksRecreationFrappe(PSARestrictedBlocksRecreation):
    """``enforce: restricted`` on the Frappe namespace, then the gunicorn pod is deleted.

    The Frappe image adds ``CAP_CHOWN`` and sets no seccomp profile or
    ``runAsNonRoot``/``allowPrivilegeEscalation``, so the ReplicaSet's
    replacement pod is rejected at admission and the Desk API has no backend.
    """

    def __init__(self, app_name: str = APP, faulty_service: str = GUNICORN):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self._prior_psa_labels = {}
        configure_port(
            self,
            app_name,
            component=f"namespace/{APP}",
            description=(
                f"The `{APP}` namespace carries the Pod Security Admission label "
                f"`{PSA_ENFORCE_LABEL}={RESTRICTED_PROFILE}`, so the kube-apiserver enforces the restricted Pod "
                "Security Standard at admission. Frappe's pods add the `CAP_CHOWN` capability and set no seccomp "
                "profile, `runAsNonRoot` or `allowPrivilegeEscalation: false`, so they violate the restricted "
                f"profile. Running pods were unaffected, but the `{faulty_service}` pod (gunicorn behind "
                f"Service `{WEB_SERVICE}`) was deleted and its ReplicaSet's replacement is rejected with "
                '`violates PodSecurity "restricted"`, so the deployment has no ready replica and every Desk/API '
                "request fails although the Deployment spec, image and Service are healthy. No admission webhook "
                "is involved. Mitigation: remove the enforce label, relax it to a profile the workload satisfies, "
                "or make the workload compliant."
            ),
            oracle_factory=DeploymentReadinessOracle,
        )
        self.core_api = client.CoreV1Api()

    @mark_fault_injected
    def inject_fault(self):
        self._capture_prior_psa_labels()
        self._patch_namespace_labels({PSA_ENFORCE_LABEL: RESTRICTED_PROFILE, PSA_ENFORCE_VERSION_LABEL: "latest"})
        selector = selector_of(self, self.faulty_service)
        pods = self.core_api.list_namespaced_pod(self.namespace, label_selector=selector).items
        if not pods:
            raise RuntimeError(f"No pods found for {self.faulty_service}")
        for pod in pods:
            self.core_api.delete_namespaced_pod(
                pod.metadata.name, self.namespace, body=client.V1DeleteOptions(grace_period_seconds=0)
            )
        print(f"Labelled {self.namespace} restricted and deleted {[p.metadata.name for p in pods]}")


class ConfigMapDriftFrappe(Problem):
    """gunicorn reads the site's ``site_config.json`` from a ConfigMap that has drifted.

    The site config (database name, user, password, ...) is mounted onto
    gunicorn from ConfigMap ``erp-site-config`` (subPath), and that ConfigMap
    lost ``db_password``: gunicorn's database logins are refused, so every
    request fails while the workers (reading the intact file from the shared
    volume) are fine. (Other keys are masked by fallbacks: ``db_host`` is
    repeated in the bench-wide config, ``db_name`` defaults from ``db_user``
    and a missing Redis cache URL only degrades caching.)
    """

    CONFIGMAP = "erp-site-config"
    KEY = "site_config.json"
    VOLUME = "site-config"
    MOUNT_PATH = f"/home/frappe/frappe-bench/sites/{Frappe.SITE_NAME}/site_config.json"
    DROPPED_KEYS = ("db_password",)

    def __init__(self, app_name: str = APP, faulty_service: str = GUNICORN, container: str = "gunicorn"):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.container = container
        self.expected_keys: list[str] = []
        configure_port(
            self,
            app_name,
            component=f"configmap/{self.CONFIGMAP}",
            description=(
                f"ConfigMap `{self.CONFIGMAP}`, which deployment `{faulty_service}` mounts (subPath) as the Frappe "
                f"site's `sites/{Frappe.SITE_NAME}/site_config.json`, has drifted and is missing a required key "
                f"({', '.join(f'`{k}`' for k in self.DROPPED_KEYS)}, the site account's MariaDB password). Gunicorn "
                "therefore starts with incomplete runtime config: its database logins are refused (`Access denied "
                f"for user ... (using password: NO)`), so it fails readiness and every Desk/API request behind "
                f"`{WEB_SERVICE}`, while the RQ workers, which read the intact file from the shared volume, keep "
                "working. Fix: restore the missing key in the ConfigMap (and restart gunicorn, since a subPath mount "
                "is not refreshed), or stop mounting the drifted copy."
            ),
            oracle_factory=FrappeFaultStateOracle,
        )

    def _read_file(self, target: str) -> dict:
        return json.loads(self.app.exec_in(target, f"cat {self.MOUNT_PATH}", container=self.container))

    @mark_fault_injected
    def inject_fault(self):
        config = self._read_file(f"deploy/{self.faulty_service}")
        self.expected_keys = sorted(config)
        state_file(self, "configmap-drift").write_text(json.dumps(self.expected_keys))
        drifted = {k: v for k, v in config.items() if k not in self.DROPPED_KEYS}
        apply_manifest(
            self,
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": self.CONFIGMAP, "namespace": self.namespace},
                "data": {self.KEY: json.dumps(drifted, indent=1)},
            },
        )
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{"name": self.VOLUME, "configMap": {"name": self.CONFIGMAP}}],
                        "containers": [
                            {
                                "name": self.container,
                                "volumeMounts": [
                                    {
                                        "name": self.VOLUME,
                                        "mountPath": self.MOUNT_PATH,
                                        "subPath": self.KEY,
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                    }
                }
            }
        }
        replace_pods_with_patch(self, self.faulty_service, patch)
        print(f"{self.faulty_service} reads {self.MOUNT_PATH} from {self.CONFIGMAP} without {self.DROPPED_KEYS}")

    @mark_fault_injected
    def recover_fault(self):
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [{"name": self.VOLUME, "$patch": "delete"}],
                        "containers": [
                            {
                                "name": self.container,
                                "volumeMounts": [{"mountPath": self.MOUNT_PATH, "$patch": "delete"}],
                            }
                        ],
                    }
                }
            }
        }
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=strategic "
            f"-p {shlex.quote(json.dumps(patch))}"
        )
        rollout_status(self, self.faulty_service)
        self.kubectl.exec_command(f"kubectl delete configmap {self.CONFIGMAP} -n {self.namespace} --ignore-not-found")

    def fault_check(self, oracle) -> dict | None:
        expected = self.expected_keys or json.loads(state_file(self, "configmap-drift").read_text())
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        mounts_cm = any(
            v.config_map and v.config_map.name == self.CONFIGMAP for v in dep.spec.template.spec.volumes or []
        )
        if mounts_cm:
            cm = self.kubectl.core_v1_api.read_namespaced_config_map(self.CONFIGMAP, self.namespace)
            missing = [k for k in expected if k not in json.loads((cm.data or {}).get(self.KEY) or "{}")]
            if missing:
                print(f"❌ ConfigMap {self.CONFIGMAP} is still missing {missing}")
                return oracle.fail("fault_still_present", configmap=self.CONFIGMAP, missing_keys=missing)
        selector = selector_of(self, self.faulty_service)
        pods = self.kubectl.core_v1_api.list_namespaced_pod(self.namespace, label_selector=selector).items
        for pod in pods:
            if pod.status.phase != "Running" or pod.metadata.deletion_timestamp:
                continue
            missing = [k for k in expected if k not in self._read_file(f"pod/{pod.metadata.name}")]
            if missing:
                print(f"❌ {pod.metadata.name} still runs without {missing}")
                return oracle.fail("fault_still_present", pod=pod.metadata.name, missing_keys=missing)
        print("✅ gunicorn runs with the complete site_config.json")
        return None


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
        super().__init__(app=make_app(app_name))
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
        self.probe_host_header = site_host
        self.cpu_limit = "100m"
        self.memory_limit = "65Mi"
        self.process_marker = "edge-traffic-replay"
        self.driver_interval_seconds = 0.05
        self.driver_log = "/tmp/edge-traffic-replay.log"
        self.driver_pid = "/tmp/edge-traffic-replay.pid"
        self._baseline_template = None
        self.app.configure(
            {
                "loadgen": {"target": f"http://{service_name}:{listen_port}"},
                "erpnext": {"nginx": {"environment": {"frappeSiteNameHeader": site_host}}},
            }
        )
        configure_port(
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
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=300s", timeout=330
        )
