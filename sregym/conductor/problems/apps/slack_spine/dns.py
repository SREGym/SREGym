"""Slack Spine: dns."""

from __future__ import annotations

import json
import shlex
import time

from sregym.conductor.oracles.apps.slack_spine.dns import StaleHostAliasesOracle
from sregym.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle
from sregym.conductor.problems.apps.slack_spine.helpers import (
    APP,
    SEND_HEAVY_ACTION_WEIGHTS,
    delete_pods,
    selector_of,
    wait_rollout,
)
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.environment import (
    configure_port,
    reopen_connections,
)
from sregym.conductor.problems.service_dns_resolution_failure import ServiceDNSResolutionFailure
from sregym.conductor.problems.stale_coredns_config import StaleCoreDNSConfig
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.utils.decorators import mark_fault_injected


class StaleCoreDNSConfigSlack(StaleCoreDNSConfig):
    """Cluster-wide NXDOMAIN template for ``svc.cluster.local`` in CoreDNS.

    Established connections survive the change, so svc-message (the users'
    entry point for history and sends) is replaced under the fault: its new
    pod cannot resolve ``db`` and never starts serving, and new client
    connections to every Service fail at name resolution.
    """

    def __init__(self, faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        configure_port(
            self,
            APP,
            component="configmap/coredns",
            root_cause_namespace="kube-system",
            description=(
                "The CoreDNS Corefile (ConfigMap `coredns` in `kube-system`) carries a stale `template ANY ANY "
                "svc.cluster.local` block that answers NXDOMAIN for every name matching `.*\\.svc\\.cluster\\.local`, "
                "inserted before the `kubernetes` plugin. Every in-cluster Service name therefore resolves as "
                "non-existent, cluster-wide, although CoreDNS, the Services and the application pods are healthy. "
                "In Slack Spine, new connections between roles (svc-message -> svc-auth/svc-channel, roles -> `db`, "
                "`redis`, `kafkagate`) fail at name resolution, and the restarted `svc-message` pod cannot resolve "
                "`db`, so its init keeps retrying and it never becomes Ready: message history and sends fail. "
                "Mitigation: remove the stale template from the Corefile and let CoreDNS reload/restart."
            ),
            oracle_factory=DNSResolutionMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        self.injector._inject(fault_type="stale_coredns_config", microservices=None)
        # Kept-alive connections (and svc-message's DB pool) survive the
        # Corefile change; a replaced pod has to resolve its dependencies.
        delete_pods(self, self.faulty_service)
        print(f"Injected stale CoreDNS config; replaced {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector = VirtualizationFaultInjector(namespace=self.namespace)
        self.injector._recover(fault_type="stale_coredns_config", microservices=None)
        wait_rollout(self, self.faulty_service, timeout_s=300)
        print(f"Recovered from stale CoreDNS config | Namespace: {self.namespace}\n")


class StaleHostAliasesSlack(Problem):
    """Simplified port: a paused partial rollout keeps one svc-message pod with a stale hostAliases resolver.

    The original kept an old frontend-proxy pod (paused rollout) whose
    ``hostAliases`` pinned ``frontend`` to a retained clone, splitting cart
    histories at a store cutover. Here svc-message runs two replicas; the old
    template pins ``svc-auth`` to a legacy entrypoint Service
    (``svc-auth-legacy``) via ``hostAliases``, the corrected template (no
    alias) rolled out to one pod and the rollout was paused. The legacy
    entrypoint was then retired (no endpoints), so the stale pod's session
    checks fail and about half of all sends return 503. The cart/Jaeger
    history reconstruction of the original is dropped.
    """

    EDGE_REPLICAS = 2
    TARGET_BACKEND = "svc-auth"
    LEGACY_SERVICE = "svc-auth-legacy"
    load_profile = (
        "lite_slack_sendheavy",
        {"base": "slack_session", "soak_cycles": 2, "action_weights": SEND_HEAVY_ACTION_WEIGHTS},
    )

    def __init__(self, faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        self.saved: dict | None = None
        configure_port(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"Deployment `{faulty_service}` runs {self.EDGE_REPLICAS} replicas but its rollout is paused half "
                "way: one pod runs the corrected template, the other still runs the previous ReplicaSet's template, "
                f"whose `hostAliases` entry pins `{self.TARGET_BACKEND}` to the ClusterIP of the legacy entrypoint "
                f"Service `{self.LEGACY_SERVICE}`. That entrypoint has since been retired (its selector matches no "
                "pod, so it has no endpoints). The stale pod's /etc/hosts overrides DNS, so its session checks "
                f"(`POST {self.TARGET_BACKEND}/validate`) and session mints go to a dead address and the sends it "
                "serves fail with 503 `auth_unavailable`, while its sibling, svc-auth itself and DNS are healthy and "
                "both pods are Ready behind the same Service. The root cause is the stale pod-local resolver entry "
                "surviving the paused rollout, not DNS or svc-auth. Mitigation: resume/complete the rollout (or "
                "otherwise remove the stale alias from every serving pod) so every svc-message pod resolves "
                "svc-auth through its Service, keeping both replicas and without reviving the retired entrypoint."
            ),
            oracle_factory=StaleHostAliasesOracle,
        )
        self.app.set_load_profile(*self.load_profile)

    def _patch(self, patch: dict) -> None:
        self.kubectl.exec_command_checked(
            f"kubectl patch deployment {self.faulty_service} -n {self.namespace} --type=merge "
            f"-p {shlex.quote(json.dumps(patch))}"
        )

    def _pods(self) -> list:
        return [
            pod
            for pod in self.kubectl.core_v1_api.list_namespaced_pod(
                self.namespace, label_selector=selector_of(self, self.faulty_service)
            ).items
            if pod.metadata.deletion_timestamp is None
        ]

    @staticmethod
    def _ready(pod) -> bool:
        return any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        dep = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        auth = self.kubectl.get_deployment(self.TARGET_BACKEND, self.namespace)
        self.saved = {"replicas": dep.spec.replicas or 1}
        legacy = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": self.LEGACY_SERVICE, "namespace": self.namespace},
            "spec": {
                "selector": dict(auth.spec.selector.match_labels),
                "ports": [{"name": "http", "port": 8000, "targetPort": 8000}],
            },
        }
        self.kubectl.exec_command_checked(f"kubectl apply -n {self.namespace} -f -", input_data=json.dumps(legacy))
        legacy_ip = self.kubectl.core_v1_api.read_namespaced_service(
            self.LEGACY_SERVICE, self.namespace
        ).spec.cluster_ip
        # The previous release: two replicas resolving svc-auth through the legacy entrypoint.
        self._patch(
            {
                "spec": {
                    "paused": False,
                    "replicas": self.EDGE_REPLICAS,
                    "minReadySeconds": 0,
                    "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                    "template": {"spec": {"hostAliases": [{"ip": legacy_ip, "hostnames": [self.TARGET_BACKEND]}]}},
                }
            }
        )
        wait_rollout(self, self.faulty_service, timeout_s=300, check=True)
        # The corrected release (no alias) reaches one pod; the rollout is
        # held there (minReadySeconds) and paused, as the original did.
        self._patch({"spec": {"minReadySeconds": 120, "template": {"spec": {"hostAliases": None}}}})
        deadline = time.monotonic() + 180
        while True:
            pods = [pod for pod in self._pods() if self._ready(pod)]
            aliased = [pod for pod in pods if pod.spec.host_aliases]
            if len(pods) == self.EDGE_REPLICAS and len(aliased) == 1:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("svc-message rollout did not reach one stale and one corrected Ready pod")
            time.sleep(2)
        self._patch({"spec": {"paused": True, "minReadySeconds": 0}})
        # Retire the legacy entrypoint, then replace both pods one after the
        # other (each ReplicaSet recreates its own from its template): the stale
        # pod has to open new connections through the retired address, and
        # clients' kept-alive connections re-spread over the two pods.
        retire = [{"op": "replace", "path": "/spec/selector", "value": {"retired-entrypoint": "true"}}]
        self.kubectl.exec_command_checked(
            f"kubectl patch service {self.LEGACY_SERVICE} -n {self.namespace} --type=json -p {shlex.quote(json.dumps(retire))}"
        )
        for victim in sorted(pods, key=lambda pod: not pod.spec.host_aliases):  # the stale pod first
            self.kubectl.core_v1_api.delete_namespaced_pod(victim.metadata.name, self.namespace)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                ready = [pod for pod in self._pods() if self._ready(pod)]
                names = {pod.metadata.name for pod in ready}
                if (
                    len(ready) == self.EDGE_REPLICAS
                    and victim.metadata.name not in names
                    and sum(1 for pod in ready if pod.spec.host_aliases) == 1
                ):
                    break
                time.sleep(2)
        print(f"Paused {self.faulty_service} with one pod pinning {self.TARGET_BACKEND} to retired {legacy_ip}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._patch({"spec": {"paused": False, "minReadySeconds": 0, "template": {"spec": {"hostAliases": None}}}})
        wait_rollout(self, self.faulty_service, timeout_s=300)
        self.kubectl.exec_command(
            f"kubectl delete service {self.LEGACY_SERVICE} -n {self.namespace} --ignore-not-found"
        )


class ServiceDNSResolutionFailureIA(ServiceDNSResolutionFailure):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-thread"):
        self.faulty_service = faulty_service
        configure_port(
            self,
            app_name,
            component="configmap/coredns",
            root_cause_namespace="kube-system",
            description=(
                f"CoreDNS (kube-system/coredns Corefile) is configured with an NXDOMAIN template for "
                f"`{faulty_service}.<namespace>.svc.cluster.local`, so in-cluster lookups for this service name "
                "fail at DNS resolution time. Clients that call it by name cannot resolve it even though its pods "
                "are healthy and listening, so thread requests fail with name-resolution errors."
            ),
            oracle_factory=DNSResolutionMitigationOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        super().inject_fault()
        # Clients (the load generator's thread/reply sessions) must re-resolve the name.
        reopen_connections(self, self.faulty_service)
