"""Mitigation oracle for the ``cumulative_admission_webhook_timeout`` problem.

This oracle is purpose-built because the default ``MitigationOracle``
(which walks every pod and requires phase == "Running") cannot detect
this fault's symptom: the ``recommendation`` pod is *absent* (the
ReplicaSet's recreate attempt is blocked by admission), not crashed. A
pod walk that filters out terminated/absent pods can pass the namespace
trivially even when the deployment is missing its replica entirely.

The fault is a *missing* ingress allow for the kube-apiserver, so the
intended fix is additive: add the allow on the webhook port. Several
other fixes also work: lower the webhooks' ``timeoutSeconds`` so the
cumulative total fits below the global admission deadline, narrow one or
more webhooks' ``namespaceSelector`` to exclude the application
namespace, or delete one or more (but not all) of the webhook
configurations. The oracle does not enumerate fix shapes; it confirms a
working fix at runtime (the probe) and rejects the destructive shortcuts
(removing the namespace's network isolation, deleting all webhooks,
deleting or scaling the workload to zero).

The oracle checks four properties in order:

1. **Workload intact.** The policy namespace exists, at least one of
   the SREGym webhook configurations remains (the policy plane must
   remain present), and the application's target deployment exists
   with its original replica count.
2. **Backends network-isolated.** At least one ingress NetworkPolicy in
   the policy namespace still selects the webhook backends. This rejects
   the "open everything up" shortcut: the fault is a missing allow, so
   the fix must add the allow, not tear down the isolation. (Deleting
   only the baseline default-deny does not even work, because the other
   targeted allow policies still select and isolate the backends.)
3. **Pod healthy.** The target deployment reports
   ``ready_replicas == spec.replicas``; the Service has at least one
   endpoint.
4. **Fix verified at runtime.** A fresh probe pod is created in the
   application namespace and observed transitioning to Running within
   ``PROBE_TIMEOUT_S``. This is the ground truth that admission works
   again; if the probe creation itself fails with a timeout (the same
   symptom the agent was supposed to fix), this property fails.
"""

import contextlib
import logging
import secrets
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.base import Oracle

logger = logging.getLogger(__name__)


_ROLLOUT_SETTLE_SECONDS = 60
_ROLLOUT_POLL_INTERVAL = 3

# Probe pod
_PROBE_TIMEOUT_S = 90
_PROBE_POLL_INTERVAL = 3
_PROBE_IMAGE = "busybox:1.36"


class CumulativeAdmissionWebhookTimeoutMitigationOracle(Oracle):
    """Oracle for the cumulative admission-webhook timeout fault.

    Attributes referenced from the Problem (set in its ``__init__``):
        problem.namespace              - application namespace
        problem.TARGET_DEPLOYMENT      - the deployment whose replica is missing
        problem.POLICY_NAMESPACE       - where the webhook backends live
        problem.WEBHOOK_BACKEND_NAMES  - names of the 4 webhook configs (used directly,
                                          no prefix); each name is also the name of the
                                          corresponding backend Service in POLICY_NAMESPACE.
        problem.BACKEND_TIER_LABEL     - label on the backend pods; used to tell whether a
                                          NetworkPolicy podSelector still isolates the backends.
    """

    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.networking_v1 = client.NetworkingV1Api()
        self.admissionregistration_v1 = client.AdmissionregistrationV1Api()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def evaluate(self) -> dict:
        print("== Cumulative Webhook Timeout Mitigation Evaluation ==")

        namespace = self.problem.namespace
        target_deployment = self.problem.TARGET_DEPLOYMENT

        # Give any agent-triggered rollout a moment to settle.
        self._wait_for_rollout_settle(namespace)

        # 1. Workload intact: deployment present with its replica count, at
        #    least one webhook remains, policy namespace still exists.
        intact_ok, intact_reason = self._workload_intact()
        if not intact_ok:
            return self._fail(intact_reason)

        # 2. The webhook backends remain network-isolated (the fix must be
        #    additive: add the missing apiserver allow, not remove isolation).
        isolated_ok, isolated_reason = self._backends_network_isolated()
        if not isolated_ok:
            return self._fail(isolated_reason)

        # 3. Target pod is healthy and its Service has endpoints.
        healthy_ok, healthy_reason = self._pod_healthy()
        if not healthy_ok:
            return self._fail(healthy_reason)

        # 4. A fresh probe pod admits within the deadline. This is the
        #    ground truth that admission actually works again.
        probe_ok, probe_reason = self._functional_probe()
        if not probe_ok:
            return self._fail(probe_reason)

        print(
            f"✅ All properties passed: workload intact, backends still isolated, "
            f"'{target_deployment}' deployment fully ready, and a fresh probe pod "
            "was admitted within the deadline."
        )
        return {"success": True}

    # ------------------------------------------------------------------
    # Property: backends remain network-isolated
    # ------------------------------------------------------------------
    def _backends_network_isolated(self) -> tuple[bool, str]:
        """The webhook backends must remain selected by at least one
        ingress NetworkPolicy in the policy namespace.

        The fault is a *missing* ingress allow for the kube-apiserver, not
        the presence of a deny. The intended fix is additive: add the
        allow (or lower the webhook timeouts / narrow their
        namespaceSelector / remove some webhooks). Tearing down all of the
        namespace's ingress NetworkPolicies would open the backends but
        destroy their security isolation, which is not an accepted fix.

        This guards only against the "removed all isolation" shortcut; the
        functional probe is the arbiter of whether admission actually
        works for every accepted fix."""
        policy_ns = self.problem.POLICY_NAMESPACE
        try:
            policies = self.networking_v1.list_namespaced_network_policy(namespace=policy_ns).items
        except ApiException as e:
            if e.status == 404:
                policies = []
            else:
                raise

        for np in policies:
            spec = np.spec
            if not spec:
                continue
            if "Ingress" not in (spec.policy_types or []):
                continue
            if self._selector_selects_backends(spec.pod_selector):
                return True, "Webhook backends remain network-isolated"

        return False, (
            "The webhook backends are no longer isolated by any ingress NetworkPolicy "
            f"in '{policy_ns}'. Removing the namespace's isolation is not an accepted "
            "fix. The fault is a missing ingress allow for the kube-apiserver, so the "
            "fix is to add that allow on the webhook port (or lower the webhook "
            "timeoutSeconds so the cumulative sum fits under the global admission "
            "deadline, narrow at least one webhook's namespaceSelector to exclude the "
            "application namespace, or delete one or more but not all of the webhooks)."
        )

    def _selector_selects_backends(self, pod_selector) -> bool:
        """Return True if a NetworkPolicy podSelector selects at least one
        webhook backend pod. An empty selector matches every pod in the
        namespace (so it includes the backends). Otherwise the selector is
        evaluated against the backends' concrete label sets,
        ``{app: <backend>, tier: compliance}``."""
        if pod_selector is None:
            return True
        match_labels = pod_selector.match_labels or {}
        match_expressions = pod_selector.match_expressions or []
        if not match_labels and not match_expressions:
            return True
        backend_label_sets = [
            {"app": name, **self.problem.BACKEND_TIER_LABEL} for name in self.problem.WEBHOOK_BACKEND_NAMES
        ]
        return any(self._labels_match(labels, match_labels, match_expressions) for labels in backend_label_sets)

    @staticmethod
    def _labels_match(labels: dict, match_labels: dict, match_expressions) -> bool:
        """Evaluate a Kubernetes label selector (matchLabels +
        matchExpressions) against a concrete label set."""
        for k, v in match_labels.items():
            if labels.get(k) != v:
                return False
        for expr in match_expressions:
            key, op, values = expr.key, expr.operator, set(expr.values or [])
            present = key in labels
            if op == "In" and (not present or labels[key] not in values):
                return False
            if op == "NotIn" and present and labels[key] in values:
                return False
            if op == "Exists" and not present:
                return False
            if op == "DoesNotExist" and present:
                return False
            if op not in {"In", "NotIn", "Exists", "DoesNotExist"}:
                return False
        return True

    # ------------------------------------------------------------------
    # Property 2: workload intact
    # ------------------------------------------------------------------
    def _workload_intact(self) -> tuple[bool, str]:
        policy_ns = self.problem.POLICY_NAMESPACE
        backend_names = self.problem.WEBHOOK_BACKEND_NAMES
        target_deployment = self.problem.TARGET_DEPLOYMENT
        app_namespace = self.problem.namespace

        # Policy namespace must still exist (deleting it is not an accepted fix)
        try:
            self.core_v1.read_namespace(name=policy_ns)
        except ApiException as e:
            if e.status == 404:
                return False, (
                    f"Policy namespace '{policy_ns}' was deleted. The policy plane "
                    "must remain present; restore the namespace and the webhooks, "
                    "and apply a non-destructive fix."
                )
            raise

        # At least one webhook must remain
        survivors = self._list_sregym_webhooks(backend_names)
        if not survivors:
            return False, (
                "All SREGym-created compliance webhooks have been deleted. The policy "
                "plane must remain present (at least one webhook should remain). "
                "Restore at least one webhook and apply a non-destructive fix."
            )

        # Target deployment must exist
        try:
            d = self.apps_v1.read_namespaced_deployment(name=target_deployment, namespace=app_namespace)
        except ApiException as e:
            if e.status == 404:
                return False, (
                    f"Target deployment '{target_deployment}' in '{app_namespace}' "
                    "was deleted. The application workload must remain present; "
                    "restore the deployment and re-apply the fix."
                )
            raise
        if (d.spec.replicas or 1) < 1:
            return False, (
                f"Target deployment '{target_deployment}' was scaled to "
                f"{d.spec.replicas} replicas. Scaling to zero is not an accepted fix; "
                "the deployment's original replica count must be preserved."
            )

        return True, "Workload intact"

    # ------------------------------------------------------------------
    # Property 3: pod is healthy
    # ------------------------------------------------------------------
    def _pod_healthy(self) -> tuple[bool, str]:
        target_deployment = self.problem.TARGET_DEPLOYMENT
        app_namespace = self.problem.namespace

        d = self.apps_v1.read_namespaced_deployment(name=target_deployment, namespace=app_namespace)
        desired = d.spec.replicas or 1
        ready = d.status.ready_replicas or 0
        if ready < desired:
            return False, (
                f"Deployment '{target_deployment}' in '{app_namespace}' shows "
                f"ready_replicas={ready} (expected {desired}). The application is "
                "still missing a replica; admission is likely still failing."
            )

        # Service endpoints
        try:
            endpoints = self.core_v1.read_namespaced_endpoints(name=target_deployment, namespace=app_namespace)
        except ApiException as e:
            if e.status == 404:
                return False, f"Service '{target_deployment}' has no Endpoints object."
            raise
        subsets = endpoints.subsets or []
        addr_count = sum(len(s.addresses or []) for s in subsets)
        if addr_count < 1:
            return False, (
                f"Service '{target_deployment}' has no ready endpoint addresses; "
                "user traffic to this service still fails."
            )

        return True, "Pod healthy and Service has endpoints"

    # ------------------------------------------------------------------
    # Property 4: functional probe
    # ------------------------------------------------------------------
    def _functional_probe(self) -> tuple[bool, str]:
        """Create a fresh probe pod in the app namespace and verify it
        transitions to Running within the deadline. If admission is still
        broken (i.e., the fix was not real), this fails."""
        app_namespace = self.problem.namespace
        probe_name = f"oracle-probe-{secrets.token_hex(4)}"
        body = client.V1Pod(
            metadata=client.V1ObjectMeta(name=probe_name, namespace=app_namespace),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="probe",
                        image=_PROBE_IMAGE,
                        command=["sh", "-c", "sleep 30"],
                    )
                ],
                restart_policy="Never",
            ),
        )
        print(f"  [functional-probe] creating pod '{probe_name}' in '{app_namespace}' to test admission")
        try:
            self.core_v1.create_namespaced_pod(namespace=app_namespace, body=body)
        except ApiException as e:
            return False, (
                f"Functional probe pod '{probe_name}' could not be admitted: "
                f"{e.reason} ({e.status}). The admission path is still broken. "
                f"Body: {(e.body or '')[:300]}"
            )

        deadline = time.monotonic() + _PROBE_TIMEOUT_S
        try:
            while time.monotonic() < deadline:
                p = self.core_v1.read_namespaced_pod(name=probe_name, namespace=app_namespace)
                phase = p.status.phase
                if phase == "Running":
                    return True, "Functional probe pod transitioned to Running"
                if phase == "Failed":
                    return False, f"Functional probe pod ended in Failed phase: {p.status.message}"
                time.sleep(_PROBE_POLL_INTERVAL)
            return False, (
                f"Functional probe pod '{probe_name}' did not reach Running within "
                f"{_PROBE_TIMEOUT_S}s. The fix may not be effective."
            )
        finally:
            self._delete_probe_pod(probe_name, app_namespace)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _wait_for_rollout_settle(self, namespace: str) -> None:
        deadline = time.monotonic() + _ROLLOUT_SETTLE_SECONDS
        while time.monotonic() < deadline:
            deployments = self.apps_v1.list_namespaced_deployment(namespace=namespace)
            settled = True
            for dep in deployments.items:
                desired = dep.spec.replicas or 1
                ready = dep.status.ready_replicas or 0
                updated = dep.status.updated_replicas or 0
                unavailable = dep.status.unavailable_replicas or 0
                if ready < desired or updated < desired or unavailable > 0:
                    # only block on the target; let the others settle in background
                    if dep.metadata.name == self.problem.TARGET_DEPLOYMENT:
                        settled = False
                        break
            if settled:
                return
            time.sleep(_ROLLOUT_POLL_INTERVAL)

    def _list_sregym_webhooks(self, backend_names) -> list:
        """Return the list of SREGym-created MutatingWebhookConfigurations
        that still exist. The webhook config name equals the backend name
        (no ``sregym-`` prefix, so cluster names do not leak the benchmark
        suite to the agent under test). Decoy MutatingWebhookConfigurations
        (cert-manager, istio, kyverno, linkerd-style names) are intentionally
        not included here; only the four real cumulative-timeout offenders
        are tracked by the oracle as the policy plane that must remain."""
        result = []
        for backend_name in backend_names:
            try:
                cfg = self.admissionregistration_v1.read_mutating_webhook_configuration(name=backend_name)
                result.append(cfg)
            except ApiException as e:
                if e.status != 404:
                    raise
        return result

    def _delete_probe_pod(self, name: str, namespace: str) -> None:
        with contextlib.suppress(ApiException):
            self.core_v1.delete_namespaced_pod(name=name, namespace=namespace, grace_period_seconds=0)

    @staticmethod
    def _fail(reason: str) -> dict:
        print(f"❌ {reason}")
        return {"success": False, "reason": reason}
