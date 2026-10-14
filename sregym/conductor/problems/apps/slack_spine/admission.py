"""Slack Spine: admission."""

from __future__ import annotations

import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.admission_webhook_outage_mitigation import AdmissionWebhookOutageMitigationOracle
from sregym.conductor.oracles.cumulative_admission_webhook_timeout_mitigation import (
    CumulativeAdmissionWebhookTimeoutMitigationOracle,
)
from sregym.conductor.oracles.mutating_webhook_resource_limits_mitigation import (
    MutatingWebhookResourceLimitsMitigationOracle,
)
from sregym.conductor.problems.admission_webhook_tls_mismatch import AdmissionWebhookTLSMismatch
from sregym.conductor.problems.apps.slack_spine.helpers import (
    _EXTRA_TRUSTED_PREFIXES,
    APP,
    delete_pods,
    wait_rollout,
)
from sregym.conductor.problems.cumulative_admission_webhook_timeout_hotel_reservation import (
    CumulativeAdmissionWebhookTimeoutHotelReservation,
)
from sregym.conductor.problems.environment import configure_port
from sregym.conductor.problems.mutating_webhook_resource_limits import MutatingWebhookResourceLimits
from sregym.utils.decorators import mark_fault_injected


class AdmissionWebhookTLSMismatchSlack(AdmissionWebhookTLSMismatch):
    """A fail-closed pod webhook whose ``caBundle`` does not match its server certificate.

    svc-channel (one replica, on every send's authz path) loses its pod and
    the ReplicaSet's replacement is rejected with an x509 admission error.
    """

    def __init__(self, faulty_service: str = "svc-channel"):
        self.faulty_service = faulty_service
        self.service_name = faulty_service
        # The oracle proves admission with a surge rollout, so the probe itself
        # causes no outage of the single-replica role.
        self.recreation_probe = "rollout"
        self.wrong_ca_bundle = None
        configure_port(
            self,
            APP,
            component=f"ValidatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            description=(
                f"A cluster-scoped ValidatingWebhookConfiguration `{self.WEBHOOK_NAME}` with `failurePolicy: Fail` "
                "and a `namespaceSelector` scoped to the application namespace intercepts pod CREATE and calls the "
                f"reachable HTTPS service `{self.BACKEND_SVC_NAMESPACE}/{self.BACKEND_SVC_NAME}`, but its `caBundle` "
                "is stale/wrong (a different CA than the one that signed the webhook server's certificate). The "
                "kube-apiserver cannot verify the server's TLS certificate and rejects every pod creation with a "
                f"`failed calling webhook` / `x509: certificate signed by unknown authority` error. The `{faulty_service}` "
                "pod was deleted and its ReplicaSet cannot recreate it, so the deployment has no Ready replica and "
                f"Service `{faulty_service}` has no endpoints: svc-message's channel authz check fails and message "
                "sends return 503, although the deployment's own spec, image and Service are healthy. Mitigation: "
                "delete the webhook configuration, set `failurePolicy: Ignore`, or restore the correct `caBundle`."
            ),
            oracle_factory=AdmissionWebhookOutageMitigationOracle,
        )
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._ensure_webhook_backend()
        webhook = self._build_webhook_body()
        try:
            self.admission_api.create_validating_webhook_configuration(body=webhook)
        except ApiException as exc:
            if exc.status != 409:
                raise
            existing = self.admission_api.read_validating_webhook_configuration(name=self.WEBHOOK_NAME)
            webhook["metadata"]["resourceVersion"] = existing.metadata.resource_version
            self.admission_api.replace_validating_webhook_configuration(name=self.WEBHOOK_NAME, body=webhook)
        print(f"Installed ValidatingWebhookConfiguration {self.WEBHOOK_NAME} with a mismatched caBundle")
        time.sleep(2)
        if not delete_pods(self, self.faulty_service):
            raise RuntimeError(f"No pods found for {self.faulty_service}")

    @mark_fault_injected
    def recover_fault(self):
        super().recover_fault()
        wait_rollout(self, self.faulty_service, timeout_s=300)


class CumulativeAdmissionWebhookTimeoutSlack(CumulativeAdmissionWebhookTimeoutHotelReservation):
    """Four ``Ignore`` webhooks whose cumulative timeouts exceed the apiserver's admission deadline.

    The target is svc-auth (one replica): without it logins fail and every
    send fails its session check. The backends' trusted-image allowlist also
    covers Slack Spine's stock images (postgres, redpanda), so once the agent
    opens the apiserver path the now-reachable webhooks admit every app pod.
    """

    WEBHOOK_SERVER_SCRIPT = CumulativeAdmissionWebhookTimeoutHotelReservation.WEBHOOK_SERVER_SCRIPT.replace(
        '    "yinfangchen/",', '    "yinfangchen/",\n' + "\n".join(_EXTRA_TRUSTED_PREFIXES)
    )

    def __init__(self, target_deployment: str = "svc-auth"):
        self.TARGET_DEPLOYMENT = target_deployment
        self.faulty_service = target_deployment
        total = sum(self.WEBHOOK_TIMEOUTS_S.values())
        configure_port(
            self,
            APP,
            component=f"deployment/{target_deployment}",
            description=(
                "Pod creation in the application namespace fails with `Timeout: request did not complete within "
                "requested timeout - context deadline exceeded`: the kube-apiserver's global admission deadline "
                "(~30s) is exceeded by the cumulative waiting time across a chain of MutatingWebhookConfigurations "
                f"({', '.join(self.WEBHOOK_BACKEND_NAMES)}) that target this namespace via `namespaceSelector`, each "
                f"with `failurePolicy: Ignore` and uneven `timeoutSeconds` summing to about {total}s. Their backends "
                f"live in `{self.POLICY_NAMESPACE}` and are isolated by NetworkPolicies: a baseline default-deny "
                f"(`{self.NETWORK_POLICY_NAME}`) plus targeted allows (metrics from `{self.OBSERVE_NAMESPACE}`, "
                "intra-namespace, and a `kube-system` control-plane allow). None admits the kube-apiserver, which "
                "runs with `hostNetwork: true` and connects from a node address; the control-plane allow is a "
                "near-miss that matches kube-system pods only. Every webhook call hangs to its own timeout, so the "
                "per-webhook Ignore never applies and the error names no webhook. Companion webhooks (cert-manager, "
                "istio, kyverno, linkerd-style names) share the namespaceSelector but are inert decoys. The fault is "
                "the missing apiserver ingress allow; the fix is to add it (or otherwise bring the cumulative "
                "timeout under the deadline) while keeping the namespace isolated and the webhooks in place. "
                f"Application impact: the `{target_deployment}` pod was deleted and its ReplicaSet cannot recreate "
                f"it, so the deployment shows 0/1 ready and Service `{target_deployment}` has no endpoints: logins "
                "fail and every message send fails svc-message's session check (503 `auth_unavailable`)."
            ),
            oracle_factory=CumulativeAdmissionWebhookTimeoutMitigationOracle,
        )
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.networking_v1 = client.NetworkingV1Api()
        self.admissionregistration_v1 = client.AdmissionregistrationV1Api()

    def _delete_target_pod(self) -> None:
        delete_pods(self, self.TARGET_DEPLOYMENT)


class MutatingWebhookResourceLimitsIA(MutatingWebhookResourceLimits):
    def __init__(self, app_name: str = "slack_spine", faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        self.ca_bundle = None
        configure_port(
            self,
            app_name,
            component=f"MutatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            description=(
                f"The fault is the cluster-scoped MutatingWebhookConfiguration `{self.WEBHOOK_NAME}`. It "
                "intercepts every pod CREATE in the application namespace and patches the first container's "
                f"`resources.requests.memory` and `resources.limits.memory` to `{self.INJECTED_MEMORY}`. Four "
                "companion MutatingWebhookConfigurations share its namespaceSelector and backend Service but stay "
                "inert (rules for uninstalled CRDs, or objectSelectors requiring labels no pod carries); the "
                f"webhook backend works as configured. Deployment `{faulty_service}` declares legitimate memory "
                f"(`requests: {self.SPEC_MEMORY_REQUEST}`, `limits: {self.SPEC_MEMORY_LIMIT}`) but its recreated "
                f"pod runs with `{self.INJECTED_MEMORY}` and is OOMKilled on startup; the gap between the "
                "Deployment spec and the running pod's resources is the diagnostic signal."
            ),
            oracle_factory=MutatingWebhookResourceLimitsMitigationOracle,
        )
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()
