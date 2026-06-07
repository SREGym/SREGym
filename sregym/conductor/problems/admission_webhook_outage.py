"""Problem: admission webhook outage blocks pod admission in an app namespace.

This models the failure archetype where a ValidatingWebhookConfiguration with
``failurePolicy: Fail`` points at a backend whose pods are unavailable, so every
admission request to the webhook fails and the kube-apiserver rejects the
intercepted operations. Two well-documented public postmortems of this class
of failure are Jetstack's 2019 GKE admission-webhook outage (listed on k8s.af)
and Wyssmann Engineering's December 2022 OPA Gatekeeper / Rancher cluster
restore incident (https://wyssmann.com/blog/2022/12/opa-gatekeeper-and-issue-while-doing-a-cluster-restore/).

In SREGym we scope the failure to a single application namespace via
``namespaceSelector`` and then delete one pod of a single-replica deployment
(e.g. ``recommendation`` in hotel-reservation). The ReplicaSet's recreate
attempt hits the broken webhook and is rejected, so the deployment stays
under-replicated even though its spec, image, and resources are healthy.

Three valid mitigations: delete the webhook config, change ``failurePolicy``
to ``Ignore``, or repair the backend service. ``DeploymentReadinessOracle``
accepts any of them because each restores the affected Deployment's
``ready_replicas`` to its desired count. The generic ``MitigationOracle``
is unsuitable here: when admission blocks pod creation, the missing pod is
absent from the namespace's pod list and the per-pod walk reports false
success.
"""

import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.deployment_readiness import DeploymentReadinessOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class AdmissionWebhookOutage(Problem):
    """Inject a broken ValidatingWebhookConfiguration that blocks pod CREATE
    in the target app namespace, then delete a pod so the ReplicaSet's
    recreate attempt surfaces the admission failure."""

    APPS = {
        "hotel_reservation": HotelReservation,
        "social_network": SocialNetwork,
        "astronomy_shop": AstronomyShop,
    }

    # Names chosen to look like a real production policy webhook (Gatekeeper /
    # Kyverno / pod-security-policy style) so neither the webhook configuration
    # nor the backend service name leaks "this is a planted fault" to an agent
    # inspecting the cluster.
    WEBHOOK_NAME = "pod-policy.validation.k8s.io"
    BACKEND_SVC_NAME = "pod-policy-webhook"
    BACKEND_SVC_NAMESPACE = "policy-system"

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "recommendation"):
        if app_name not in self.APPS:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.app_name = app_name
        self.faulty_service = faulty_service
        app = self.APPS[app_name]()
        super().__init__(app=app)

        self.kubectl = KubeCtl()
        self.admission_api = client.AdmissionregistrationV1Api()
        self.core_api = client.CoreV1Api()

        self.root_cause = self.build_structured_root_cause(
            component=f"ValidatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            namespace=self.namespace,
            description=(
                f"A cluster-scoped ValidatingWebhookConfiguration named `{self.WEBHOOK_NAME}` has been installed "
                f"with `failurePolicy: Fail` and a `namespaceSelector` scoped to the `{self.namespace}` namespace. "
                "The webhook intercepts pod CREATE operations, but its `clientConfig.service` points at a backend "
                f"service `{self.BACKEND_SVC_NAMESPACE}/{self.BACKEND_SVC_NAME}` that has no endpoints, so every "
                "admission request times out and the kube-apiserver rejects the request with a `failed calling "
                f"webhook` error. As a result, the ReplicaSet controlling the `{self.faulty_service}` deployment "
                "cannot recreate pods after they are deleted, leaving the deployment under-replicated. The "
                f"`{self.faulty_service}` deployment itself is healthy — its spec, image, and resources are "
                "correct; it is an innocent victim of a cluster-scoped admission dependency. The mitigation is "
                "to remove the broken ValidatingWebhookConfiguration (alternatively change its `failurePolicy` "
                "to `Ignore`, or restore endpoints for the backend service); once admission unblocks, the "
                "existing ReplicaSet immediately recreates the missing pod without any other changes."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        # Use DeploymentReadinessOracle, not the generic MitigationOracle: the fault
        # makes the affected pod absent from the namespace's pod list, so a per-pod
        # health walk reports false success even when the fault is unmitigated.
        self.mitigation_oracle = DeploymentReadinessOracle(problem=self)

    def _build_webhook_body(self) -> dict:
        return {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "ValidatingWebhookConfiguration",
            "metadata": {"name": self.WEBHOOK_NAME},
            "webhooks": [
                {
                    "name": self.WEBHOOK_NAME,
                    "clientConfig": {
                        "service": {
                            "name": self.BACKEND_SVC_NAME,
                            "namespace": self.BACKEND_SVC_NAMESPACE,
                            "path": "/validate",
                            "port": 443,
                        },
                        # caBundle intentionally omitted: the apiserver will fall back
                        # to its system trust roots, so the failure surfaces at the
                        # backend lookup step ("no endpoints for service ..."), not at
                        # the cert parsing step. This matches the documented narrative
                        # of a webhook backend that is down.
                    },
                    "rules": [
                        {
                            "apiGroups": [""],
                            "apiVersions": ["v1"],
                            "operations": ["CREATE"],
                            "resources": ["pods"],
                            "scope": "Namespaced",
                        }
                    ],
                    "failurePolicy": "Fail",
                    "sideEffects": "None",
                    "admissionReviewVersions": ["v1"],
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": self.namespace},
                    },
                    "timeoutSeconds": 5,
                }
            ],
        }

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        webhook = self._build_webhook_body()
        try:
            self.admission_api.create_validating_webhook_configuration(body=webhook)
            print(f"Created ValidatingWebhookConfiguration: {self.WEBHOOK_NAME}")
        except ApiException as e:
            if e.status == 409:
                print(f"ValidatingWebhookConfiguration {self.WEBHOOK_NAME} exists; replacing")
                existing = self.admission_api.read_validating_webhook_configuration(name=self.WEBHOOK_NAME)
                webhook["metadata"]["resourceVersion"] = existing.metadata.resource_version
                self.admission_api.replace_validating_webhook_configuration(name=self.WEBHOOK_NAME, body=webhook)
            else:
                raise

        # Give the apiserver a moment to register the webhook before triggering
        # a pod CREATE that should hit it.
        time.sleep(2)

        pods = self.core_api.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=f"io.kompose.service={self.faulty_service}",
        )
        if not pods.items:
            raise RuntimeError(f"No pods found for service '{self.faulty_service}' in namespace '{self.namespace}'")
        target = pods.items[0].metadata.name
        self.core_api.delete_namespaced_pod(
            name=target,
            namespace=self.namespace,
            body=client.V1DeleteOptions(grace_period_seconds=0),
        )
        print(f"Deleted pod {target}; ReplicaSet recreate will be blocked by webhook")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        try:
            self.admission_api.delete_validating_webhook_configuration(name=self.WEBHOOK_NAME)
            print(f"Deleted ValidatingWebhookConfiguration: {self.WEBHOOK_NAME}")
        except ApiException as e:
            if e.status == 404:
                print(f"ValidatingWebhookConfiguration {self.WEBHOOK_NAME} already absent")
            else:
                raise
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
