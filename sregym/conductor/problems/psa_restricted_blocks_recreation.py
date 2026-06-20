"""Problem: Pod Security Admission (PSA) ``enforce: restricted`` blocks pod recreation.

This models a production-style Kubernetes hardening failure. A namespace receives
the PSA label ``pod-security.kubernetes.io/enforce: restricted`` as part of a
security rollout. Pods already running are unaffected — PSA only gates admission —
so nothing breaks immediately. But the moment a pod is recreated (node drain,
eviction, rollout, crash), the ReplicaSet's recreate attempt is rejected by the
kube-apiserver itself with ``violates PodSecurity "restricted"`` because the demo
app's pods run as root, set no seccomp profile, and do not drop capabilities. The
affected deployment stays under-replicated.

Unlike the existing webhook problems (``admission_webhook_outage``,
``admission_webhook_tls_mismatch``, ``mutating_webhook_resource_limits``), there is
no ValidatingWebhookConfiguration or MutatingWebhookConfiguration to find — PSA is
built into the apiserver and the policy lives as a label on the Namespace object.
Enumerating webhook configs returns nothing; the agent must inspect namespace
metadata, a genuinely different place to look.

We scope the fault to a single application namespace by labelling that namespace,
then delete one pod of a single-replica deployment (``recommendation`` in
hotel-reservation). The ReplicaSet's recreate attempt is denied at admission, so
the deployment stays under-replicated even though its spec, image, service, and
resources are healthy.

Valid mitigations: remove the enforce label, relax it to a profile the workload
satisfies (e.g. ``baseline``/``privileged``), or make the workload compliant
(set ``runAsNonRoot``, ``allowPrivilegeEscalation: false``, drop capabilities, and
a ``seccompProfile``). ``DeploymentReadinessOracle`` accepts any of them because
each restores the affected deployment's ``ready_replicas`` to its desired count.
"""

from kubernetes import client

from sregym.conductor.oracles.deployment_readiness import DeploymentReadinessOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

PSA_ENFORCE_LABEL = "pod-security.kubernetes.io/enforce"
PSA_ENFORCE_VERSION_LABEL = "pod-security.kubernetes.io/enforce-version"
RESTRICTED_PROFILE = "restricted"
ENFORCE_VERSION = "latest"


class PSARestrictedBlocksRecreation(Problem):
    """Label the app namespace ``enforce: restricted`` and delete a pod so the
    ReplicaSet's blocked recreate surfaces the admission rejection."""

    APPS = {
        "hotel_reservation": HotelReservation,
        "social_network": SocialNetwork,
        "astronomy_shop": AstronomyShop,
    }

    def __init__(self, app_name: str = "hotel_reservation", faulty_service: str = "recommendation"):
        if app_name not in self.APPS:
            raise ValueError(f"Unsupported app name: {app_name}")

        self.app_name = app_name
        self.faulty_service = faulty_service
        self.app = self.APPS[app_name]()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.core_api = client.CoreV1Api()
        # Remembers the namespace's PSA label values before injection so recovery
        # restores the exact original state (rather than blindly deleting labels).
        self._prior_psa_labels: dict[str, str | None] = {}

        self.root_cause = self.build_structured_root_cause(
            component=f"namespace/{self.namespace}",
            namespace=self.namespace,
            description=(
                f"The `{self.namespace}` namespace has the Pod Security Admission label "
                f"`{PSA_ENFORCE_LABEL}={RESTRICTED_PROFILE}`, which enforces the restricted "
                "Pod Security Standard at admission. The application's pods run as root, do not "
                "set a seccomp profile, allow privilege escalation, and do not drop capabilities, "
                "so they violate the restricted profile. Already-running pods are unaffected "
                "(PSA only gates admission), but when a pod is deleted the ReplicaSet's recreate "
                f'is rejected by the kube-apiserver with `violates PodSecurity "restricted"`. The '
                f"`{self.faulty_service}` deployment therefore stays under-replicated even though "
                "its spec, image, service, and resources are healthy. There is no admission webhook "
                "involved; the policy is enforced by the apiserver itself via the namespace label. "
                "Mitigation: remove the enforce label, relax it to a profile the workload satisfies, "
                "or make the workload compliant with the restricted profile."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        # DeploymentReadinessOracle, not the generic MitigationOracle: the fault makes
        # the affected pod absent from the namespace pod list, so a per-pod health walk
        # reports false success even when the fault is unmitigated.
        self.mitigation_oracle = DeploymentReadinessOracle(problem=self)

    def _capture_prior_psa_labels(self):
        ns = self.core_api.read_namespace(self.namespace)
        labels = ns.metadata.labels or {}
        self._prior_psa_labels = {
            PSA_ENFORCE_LABEL: labels.get(PSA_ENFORCE_LABEL),
            PSA_ENFORCE_VERSION_LABEL: labels.get(PSA_ENFORCE_VERSION_LABEL),
        }

    def _patch_namespace_labels(self, labels: dict[str, str | None]):
        # A merge patch with a value of None removes that label key.
        self.core_api.patch_namespace(self.namespace, {"metadata": {"labels": labels}})

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        self._capture_prior_psa_labels()
        self._patch_namespace_labels(
            {
                PSA_ENFORCE_LABEL: RESTRICTED_PROFILE,
                PSA_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
            }
        )
        print(f"Labelled namespace {self.namespace} with {PSA_ENFORCE_LABEL}={RESTRICTED_PROFILE}")

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
        print(f"Deleted pod {target}; ReplicaSet recreate will be rejected by PSA admission")
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        # Restore the exact prior label state: re-apply prior values, or remove
        # the labels (value None) if they did not exist before injection.
        restore = self._prior_psa_labels or {
            PSA_ENFORCE_LABEL: None,
            PSA_ENFORCE_VERSION_LABEL: None,
        }
        self._patch_namespace_labels(restore)
        print(f"Restored PSA labels on namespace {self.namespace}")
        # The ReplicaSet recreates the rejected pod automatically once admission
        # allows it again; the mitigation oracle polls until the deployment is ready.
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
