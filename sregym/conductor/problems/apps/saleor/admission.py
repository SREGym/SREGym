"""Saleor: admission."""

from __future__ import annotations

from kubernetes import client

from sregym.conductor.oracles.admission_webhook_outage_mitigation import AdmissionWebhookOutageMitigationOracle
from sregym.conductor.problems.admission_webhook_outage import AdmissionWebhookOutage
from sregym.conductor.problems.environment import configure_port, make_app


class AdmissionWebhookOutageIA(AdmissionWebhookOutage):
    def __init__(
        self, app_name: str = "saleor", faulty_service: str = "saleor-api", service_name: str = "svc-saleor-api"
    ):
        super().__init__(app=make_app(app_name))
        self.faulty_service = faulty_service
        self.service_name = service_name
        # saleor-api runs one replica behind the load generator: the oracle's
        # admission probe must not delete it (that outage alone would fail
        # LoadgenHealthOracle), so it proves recreation with a surge rollout.
        self.recreation_probe = "rollout"
        configure_port(
            self,
            app_name,
            component=f"ValidatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            description=(
                f"A ValidatingWebhookConfiguration `{self.WEBHOOK_NAME}` with `failurePolicy: Fail` intercepts "
                f"pod CREATE in the namespace and calls `{self.BACKEND_SVC_NAME}` in namespace "
                f"`{self.BACKEND_SVC_NAMESPACE}`, which does not exist, so every pod creation is rejected with a "
                f"`failed calling webhook` error. The ReplicaSet of the `{faulty_service}` deployment cannot "
                f"recreate a deleted pod, leaving the deployment with no Ready replica and Service "
                f"`{service_name}` without endpoints, although the deployment's own spec is healthy."
            ),
            oracle_factory=AdmissionWebhookOutageMitigationOracle,
        )
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()
