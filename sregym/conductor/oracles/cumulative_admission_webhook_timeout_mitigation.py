"""Mitigation oracle for the ``cumulative_admission_webhook_timeout`` problem.

This oracle is purpose-built because the default ``MitigationOracle``
(which walks every pod and requires phase == "Running") cannot detect
this fault's symptom: the ``recommendation`` pod is *absent* (the
ReplicaSet's recreate attempt is blocked by admission), not crashed. A
pod walk that filters out terminated/absent pods can pass the namespace
trivially even when the deployment is missing its replica entirely.

The oracle accepts any of several legitimate fix shapes. The simplest is
to open the default-deny NetworkPolicy in the policy namespace. Other
accepted fixes include lowering the webhooks' ``timeoutSeconds`` so the
cumulative total fits below the global admission deadline, narrowing one
or more webhooks' ``namespaceSelector`` to exclude the application
namespace, or deleting one or more (but not all) of the webhook
configurations. The oracle rejects shortcuts that destroy the workload
(deleting all webhooks, deleting the recommendation deployment, scaling
to zero) and shortcuts that look like a fix but leave admission still
broken under fresh pod creation.

The oracle checks four independent properties:

1. **Spec is fixed.** At least one of:
    - The default-deny NetworkPolicy in the policy namespace is absent
      (the simplest fix and what ``recover_fault`` does).
    - The default-deny NetworkPolicy now permits ingress from outside
      the namespace.
    - The sum of ``timeoutSeconds`` across all SREGym-created webhooks
      scoped to the application namespace is at most 25s, leaving a
      5-second safety margin under the ~30s global admission deadline.
    - At least one webhook's ``namespaceSelector`` no longer matches
      the application namespace.
    - At least one (but not all) of the SREGym-created webhook
      configurations has been deleted.
2. **Workload intact.** The policy namespace exists, at least one of
   the SREGym webhook configurations remains (the policy plane must
   remain present), and the application's target deployment exists
   with its original replica count.
3. **Pod healthy.** The target deployment reports
   ``ready_replicas == spec.replicas``; the Service has at least one
   endpoint.
4. **Fix verified at runtime.** A fresh probe pod is created in the
   application namespace and observed transitioning to Running within
   ``PROBE_TIMEOUT_S``. If the probe creation itself fails with a
   timeout (the same symptom the agent was supposed to fix), this
   property fails.
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

# Global admission deadline in the kube-apiserver. The upstream default
# is ~30 seconds. The safe-fix threshold is 25 seconds of cumulative
# webhook timeoutSeconds, leaving 5 seconds of margin.
_GLOBAL_ADMISSION_DEADLINE_S = 30
_CUMULATIVE_TIMEOUT_SAFE_S = 25

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
        problem.NETWORK_POLICY_NAME    - the default-deny policy
        problem.WEBHOOK_BACKEND_NAMES  - names of the 4 webhook configs (used directly,
                                          no prefix); each name is also the name of the
                                          corresponding backend Service in POLICY_NAMESPACE.
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

        # 1. Spec is fixed (any of the accepted shapes).
        spec_ok, spec_reason = self._spec_is_fixed()
        if not spec_ok:
            return self._fail(spec_reason)

        # 2. Workload is intact.
        intact_ok, intact_reason = self._workload_intact()
        if not intact_ok:
            return self._fail(intact_reason)

        # 3. Pod is healthy.
        healthy_ok, healthy_reason = self._pod_healthy()
        if not healthy_ok:
            return self._fail(healthy_reason)

        # 4. Fresh probe pod admits successfully.
        probe_ok, probe_reason = self._functional_probe()
        if not probe_ok:
            return self._fail(probe_reason)

        print(
            f"✅ All four properties passed: spec in accepted fix shape, policy plane intact, "
            f"'{target_deployment}' deployment fully ready, and a fresh probe pod "
            "was admitted within the deadline."
        )
        return {"success": True}

    # ------------------------------------------------------------------
    # Property 1: spec is fixed
    # ------------------------------------------------------------------
    def _spec_is_fixed(self) -> tuple[bool, str]:
        """At least one of: NetworkPolicy gone / opened, timeouts lowered,
        namespaceSelector narrowed, or some webhooks deleted."""
        policy_ns = self.problem.POLICY_NAMESPACE
        net_policy_name = self.problem.NETWORK_POLICY_NAME
        backend_names = self.problem.WEBHOOK_BACKEND_NAMES
        app_namespace = self.problem.namespace

        # Check NetworkPolicy
        np = self._get_network_policy(net_policy_name, policy_ns)
        if np is None:
            return True, "NetworkPolicy deleted (accepted fix shape)"
        # Allow-all check: an ingress rule with no ``from`` selector (or an empty
        # ``from`` list) is interpreted by Kubernetes as "allow traffic from any
        # source." Either form recovers admission.
        ingress = (np.spec and np.spec.ingress) or []
        if any(rule.from_ is None or rule.from_ == [] for rule in ingress):
            return True, "NetworkPolicy permits ingress from anywhere (accepted fix shape)"

        # Inspect surviving SREGym-created webhook configurations
        survivors = self._list_sregym_webhooks(backend_names)
        if not survivors:
            return False, (
                "All SREGym-created compliance webhooks have been deleted. The policy "
                "plane is required to remain present; the accepted fixes are to open "
                "the default-deny NetworkPolicy, lower webhook timeoutSeconds so the "
                "cumulative sum is under "
                f"{_CUMULATIVE_TIMEOUT_SAFE_S}s, narrow at least one webhook's "
                "namespaceSelector to exclude the application namespace, or delete "
                "one or more (but not all) of the webhooks."
            )

        # Sum surviving timeouts targeting the application namespace
        scoped_to_app = [w for w in survivors if self._webhook_targets(w, app_namespace)]
        cumulative = sum(self._sum_timeout_seconds(w) for w in scoped_to_app)
        if cumulative <= _CUMULATIVE_TIMEOUT_SAFE_S:
            return True, (
                f"Cumulative webhook timeoutSeconds = {cumulative}s, which is below the "
                f"safe ceiling of {_CUMULATIVE_TIMEOUT_SAFE_S}s (accepted fix shape)"
            )

        # Or: some webhooks dropped from the app namespace via namespaceSelector
        original_count = len(backend_names)
        scoped_count = len(scoped_to_app)
        if scoped_count < original_count:
            return True, (
                f"At least one webhook no longer matches namespaceSelector for "
                f"'{app_namespace}' ({scoped_count} of {original_count} still scoped) "
                "(accepted fix shape)"
            )

        return False, (
            "Spec is not in any accepted fix shape: the default-deny NetworkPolicy "
            f"'{net_policy_name}' in '{policy_ns}' still blocks ingress, all "
            f"{original_count} webhooks still target '{app_namespace}', and the "
            f"cumulative timeoutSeconds is {cumulative}s (must be <= "
            f"{_CUMULATIVE_TIMEOUT_SAFE_S}s to be safe under the global ~"
            f"{_GLOBAL_ADMISSION_DEADLINE_S}s admission deadline). Accepted fixes: "
            "(a) delete or open the NetworkPolicy, (b) lower webhook timeoutSeconds, "
            "(c) narrow at least one webhook's namespaceSelector, or (d) delete some "
            "(but not all) webhook configurations."
        )

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

    def _get_network_policy(self, name: str, namespace: str):
        try:
            return self.networking_v1.read_namespaced_network_policy(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

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

    @staticmethod
    def _webhook_targets(webhook_config, app_namespace: str) -> bool:
        """Return True if any of the webhook config's webhooks selects the
        application namespace via namespaceSelector matchLabels."""
        for wh in webhook_config.webhooks or []:
            ns_sel = wh.namespace_selector
            if ns_sel is None:
                continue
            match_labels = ns_sel.match_labels or {}
            if match_labels.get("kubernetes.io/metadata.name") == app_namespace:
                return True
        return False

    @staticmethod
    def _sum_timeout_seconds(webhook_config) -> int:
        """Sum the timeoutSeconds across all webhooks in a single config."""
        return sum((wh.timeout_seconds or 0) for wh in (webhook_config.webhooks or []))

    def _delete_probe_pod(self, name: str, namespace: str) -> None:
        with contextlib.suppress(ApiException):
            self.core_v1.delete_namespaced_pod(name=name, namespace=namespace, grace_period_seconds=0)

    @staticmethod
    def _fail(reason: str) -> dict:
        print(f"❌ {reason}")
        return {"success": False, "reason": reason}
