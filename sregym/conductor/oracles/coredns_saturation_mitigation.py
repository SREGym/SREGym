"""Mitigation oracle for the CoreDNS ndots-saturation problem.

Evaluates whether an agent has successfully mitigated CoreDNS saturation
caused by ndots:5 DNS query amplification from the analytics-collector
deployment in the problem's app namespace.

Evaluation priority (all checks require DNS health as a gate):

1. **DNS health** (required gate for any passing score):
   Exec into a running pod and measure actual DNS resolution time for
   ``kubernetes.default.svc.cluster.local``. 10 samples; median must be
   under 50ms and no single sample may exceed 100ms.

2. **ndots reduction** (primary fix, full credit):
   ``analytics-collector`` deployment has ``dnsConfig.options`` with
   ``ndots <= 2``. Also accepted cluster-wide (all deployments in the
   namespace or via a mutating webhook).

3. **Stress workload removed** (partial mitigation, lower credit):
   ``analytics-collector`` is deleted or scaled to 0 replicas AND DNS
   health passes. Stops the immediate flood but leaves the underlying
   ndots vulnerability unaddressed.

4. **CoreDNS health** (supporting check, not sufficient alone):
   ``deployment/coredns`` in kube-system has at least 1 ready replica
   and is not in CrashLoopBackOff.
"""

from __future__ import annotations

from statistics import median

from kubernetes import client
from kubernetes.client.rest import ApiException

from sregym.conductor.oracles.base import Oracle

_DNS_SAMPLE_COUNT = 10
_DNS_MEDIAN_THRESHOLD_MS = 50
_DNS_MAX_SINGLE_SAMPLE_MS = 100
_NDOTS_THRESHOLD = 2

_STRESS_DEPLOYMENT_NAME = "analytics-collector"
_COREDNS_DEPLOYMENT_NAME = "coredns"
_COREDNS_NAMESPACE = "kube-system"

# Preferred pods to exec into for the DNS timing probe, in priority order.
_PREFERRED_EXEC_PODS = ["frontend"]


class CoreDNSMitigationOracle(Oracle):
    """Pass when CoreDNS saturation from ndots amplification is resolved."""

    importance = 1.0

    def __init__(self, problem):
        super().__init__(problem)
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def evaluate(self) -> dict:
        print("== CoreDNS Saturation (ndots) Mitigation Evaluation ==")

        namespace = self.problem.namespace

        # --- Supporting check: CoreDNS health ---
        coredns_healthy, coredns_detail = self._check_coredns_health()
        print(coredns_detail)

        # --- Gate: DNS health via in-pod measurement ---
        dns_healthy, dns_detail = self._check_dns_health(namespace)
        print(dns_detail)

        if not dns_healthy:
            return self._fail(f"DNS health check failed, CoreDNS is still saturated. {dns_detail}")

        # --- Primary fix: ndots reduction ---
        ndots_fixed, ndots_detail = self._check_ndots_reduction(namespace)
        print(ndots_detail)

        if ndots_fixed:
            print(
                "✅ Full pass: ndots reduced to ≤ 2 on analytics-collector (or cluster-wide) AND DNS health verified."
            )
            return {
                "success": True,
                "mitigation": "ndots_reduction",
                "coredns_healthy": coredns_healthy,
                "details": ndots_detail,
            }

        # --- Partial fix: stress workload removed ---
        stress_removed, stress_detail = self._check_stress_workload_removed(namespace)
        print(stress_detail)

        if stress_removed:
            print(
                "⚠️ Partial pass: stress workload removed and DNS is healthy, "
                "but root cause (ndots:5 amplification) is unaddressed. "
                "A legitimate high-traffic service would re-trigger saturation."
            )
            return {
                "success": True,
                "mitigation": "stress_workload_removed",
                "partial": True,
                "coredns_healthy": coredns_healthy,
                "details": (
                    "Stress workload removed, DNS flood stopped. However the "
                    "underlying ndots:5 amplification is still present. This is "
                    "an acceptable but incomplete mitigation."
                ),
            }

        # --- Neither fix applied ---
        if coredns_healthy and not ndots_fixed and not stress_removed:
            return self._fail(
                "DNS health passes (possibly due to CoreDNS scaling) but neither "
                "ndots was reduced nor the stress workload was removed. The "
                "mitigation is insufficient: the cluster remains vulnerable to "
                "ndots amplification."
            )

        return self._fail(
            "No effective mitigation detected. ndots is still ≥ 5 on "
            "analytics-collector and the stress workload is still running."
        )

    # ------------------------------------------------------------------
    # 1. DNS health, in-pod timing measurement
    # ------------------------------------------------------------------
    def _check_dns_health(self, namespace: str) -> tuple[bool, str]:
        """Exec into a running pod and measure DNS resolution times.

        Returns (healthy, detail_message).
        """
        pod_name = self._find_exec_target_pod(namespace)
        if pod_name is None:
            return False, ("❌ No running pods available in the namespace to exec into for DNS health measurement.")

        # Build a shell one-liner that resolves the name N times and prints
        # each duration in milliseconds, followed by the exit status.
        # It defines a function resolve_dns that tries nslookup, getent hosts,
        # or python3 in order, returning 127 if none are found.
        # We use `date +%s%N` for nanosecond timestamps where available,
        # falling back to a Python one-liner if the pod has Python.
        dns_target = "kubernetes.default.svc.cluster.local"
        probe_script = (
            "resolve_dns() {\n"
            "  if command -v nslookup >/dev/null 2>&1; then\n"
            '    nslookup "$1" >/dev/null 2>&1\n'
            "    return $?\n"
            "  elif command -v getent >/dev/null 2>&1; then\n"
            '    getent hosts "$1" >/dev/null 2>&1\n'
            "    return $?\n"
            "  elif command -v python3 >/dev/null 2>&1; then\n"
            "    python3 -c \"import socket; socket.gethostbyname('$1')\" >/dev/null 2>&1\n"
            "    return $?\n"
            "  else\n"
            "    return 127\n"
            "  fi\n"
            "}\n"
            f"for i in $(seq 1 {_DNS_SAMPLE_COUNT}); do "
            f"START=$(date +%s%N 2>/dev/null || python3 -c "
            f'"import time; print(int(time.time()*1e9))"); '
            f"resolve_dns {dns_target}; "
            f"STATUS=$?; "
            f"END=$(date +%s%N 2>/dev/null || python3 -c "
            f'"import time; print(int(time.time()*1e9))"); '
            f'echo "$(( (END - START) / 1000000 )) $STATUS"; '
            f"done"
        )

        container_name = self._get_first_container_name(pod_name, namespace)
        cmd = (
            f"kubectl exec {pod_name} -n {namespace}"
            f"{f' -c {container_name}' if container_name else ''}"
            f" -- sh -c '{probe_script}'"
        )

        output = self.problem.kubectl.exec_command(cmd)
        return self._parse_dns_timing(output)

    def _parse_dns_timing(self, output: str) -> tuple[bool, str]:
        """Parse millisecond values and exit status from the probe output and evaluate."""
        times_ms: list[float] = []
        statuses: list[int] = []
        for line in output.strip().splitlines():
            line = line.strip()
            # Accept lines that are "duration status".
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                times_ms.append(float(parts[0]))
                statuses.append(int(parts[1]))

        if len(times_ms) < 1:
            return False, f"❌ DNS timing probe returned no valid samples. Raw output: {output.strip()[:300]}"

        # If all query options are missing, exit code is 127 for all runs.
        if all(s == 127 for s in statuses):
            return (
                False,
                f"❌ DNS timing probe failed: no query tool (nslookup, getent, python3) found in target container. Raw output: {output.strip()[:300]}",
            )

        # If DNS resolution failed, the query command exits with non-zero status.
        if any(s != 0 for s in statuses):
            return (
                False,
                f"❌ DNS timing probe failed: DNS resolution returned error status. Times: {times_ms}, Statuses: {statuses}",
            )

        median_ms = median(times_ms)
        max_ms = max(times_ms)

        detail = f"DNS timing: samples={times_ms}, median={median_ms:.0f}ms, max={max_ms:.0f}ms"

        if max_ms > _DNS_MAX_SINGLE_SAMPLE_MS:
            return False, f"❌ {detail}, single sample exceeds {_DNS_MAX_SINGLE_SAMPLE_MS}ms"

        if median_ms > _DNS_MEDIAN_THRESHOLD_MS:
            return False, f"❌ {detail}, median exceeds {_DNS_MEDIAN_THRESHOLD_MS}ms"

        return True, f"✅ {detail}"

    def _find_exec_target_pod(self, namespace: str) -> str | None:
        """Find a running pod to exec into for DNS probing.

        Tries preferred pods first (e.g. frontend), then falls back to any
        ready pod in the namespace.
        """
        try:
            pods = self.core_v1.list_namespaced_pod(namespace=namespace)
        except ApiException:
            return None

        if not pods.items:
            return None

        running_pods = [
            pod
            for pod in pods.items
            if pod.status.phase == "Running"
            and pod.status.container_statuses
            and any(cs.ready for cs in pod.status.container_statuses)
        ]

        if not running_pods:
            return None

        # Try preferred pods first.
        for preferred in _PREFERRED_EXEC_PODS:
            for pod in running_pods:
                if pod.metadata.name.startswith(preferred):
                    return pod.metadata.name

        # Fall back to any running pod (excluding analytics-collector stress pods).
        for pod in running_pods:
            if not pod.metadata.name.startswith(_STRESS_DEPLOYMENT_NAME):
                return pod.metadata.name

        # Last resort: even a stress pod will do.
        return running_pods[0].metadata.name

    def _get_first_container_name(self, pod_name: str, namespace: str) -> str | None:
        """Return the first container name for a pod, for use with kubectl exec -c."""
        try:
            pod = self.core_v1.read_namespaced_pod(pod_name, namespace)
            containers = pod.spec.containers or []
            if len(containers) > 1:
                return containers[0].name
        except ApiException:
            pass
        return None

    # ------------------------------------------------------------------
    # 2. ndots reduction check
    # ------------------------------------------------------------------
    def _check_ndots_reduction(self, namespace: str) -> tuple[bool, str]:
        """Check whether ndots has been reduced on analytics-collector or cluster-wide.

        Returns (fixed, detail_message).
        """
        # Check analytics-collector deployment spec directly.
        ac_fixed, ac_detail = self._check_deployment_ndots(_STRESS_DEPLOYMENT_NAME, namespace)
        if ac_fixed:
            return True, f"✅ {ac_detail}"

        # Check cluster-wide: if ALL deployments in the namespace have ndots ≤ threshold.
        try:
            deployments = self.apps_v1.list_namespaced_deployment(namespace=namespace)
        except ApiException as exc:
            return False, f"ℹ️ Could not list deployments: {exc}"

        if not deployments.items:
            return False, "ℹ️ No deployments found in namespace."

        all_have_ndots = True
        for dep in deployments.items:
            ndots_val = self._extract_ndots_from_deployment(dep)
            if ndots_val is None or ndots_val > _NDOTS_THRESHOLD:
                all_have_ndots = False
                break

        if all_have_ndots and len(deployments.items) > 0:
            return True, (
                f"✅ All {len(deployments.items)} deployments in namespace "
                f"have ndots ≤ {_NDOTS_THRESHOLD} (cluster-wide fix detected)."
            )

        return False, (
            f"ℹ️ ndots not reduced on {_STRESS_DEPLOYMENT_NAME} and no cluster-wide ndots fix detected. {ac_detail}"
        )

    def _check_deployment_ndots(self, deployment_name: str, namespace: str) -> tuple[bool, str]:
        """Check a single deployment's dnsConfig for ndots ≤ threshold."""
        try:
            dep = self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return False, f"Deployment '{deployment_name}' not found (deleted)."
            return False, f"Error reading deployment '{deployment_name}': {exc}"

        ndots_val = self._extract_ndots_from_deployment(dep)
        if ndots_val is not None and ndots_val <= _NDOTS_THRESHOLD:
            return True, (f"ndots={ndots_val} on deployment/{deployment_name} (≤ {_NDOTS_THRESHOLD} threshold).")

        current = f"ndots={ndots_val}" if ndots_val is not None else "ndots not set"
        return False, f"{current} on deployment/{deployment_name}."

    @staticmethod
    def _extract_ndots_from_deployment(deployment) -> int | None:
        """Extract the ndots value from a deployment's pod template dnsConfig."""
        pod_spec = deployment.spec.template.spec
        dns_config = pod_spec.dns_config
        if dns_config is None or dns_config.options is None:
            return None
        for option in dns_config.options:
            if option.name == "ndots":
                try:
                    return int(option.value)
                except (ValueError, TypeError):
                    return None
        return None

    # ------------------------------------------------------------------
    # 3. Stress workload removal check
    # ------------------------------------------------------------------
    def _check_stress_workload_removed(self, namespace: str) -> tuple[bool, str]:
        """Check if analytics-collector is deleted or scaled to zero.

        Returns (removed, detail_message).
        """
        try:
            dep = self.apps_v1.read_namespaced_deployment(_STRESS_DEPLOYMENT_NAME, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return True, (f"✅ Stress workload '{_STRESS_DEPLOYMENT_NAME}' has been deleted.")
            return False, (f"ℹ️ Error checking stress workload: {exc}")

        replicas = dep.spec.replicas or 0
        if replicas == 0:
            return True, (f"✅ Stress workload '{_STRESS_DEPLOYMENT_NAME}' has been scaled to 0 replicas.")

        return False, (f"ℹ️ Stress workload '{_STRESS_DEPLOYMENT_NAME}' is still running with {replicas} replica(s).")

    # ------------------------------------------------------------------
    # 4. CoreDNS health (supporting check)
    # ------------------------------------------------------------------
    def _check_coredns_health(self) -> tuple[bool, str]:
        """Verify CoreDNS deployment in kube-system is healthy.

        Returns (healthy, detail_message).
        """
        try:
            dep = self.apps_v1.read_namespaced_deployment(_COREDNS_DEPLOYMENT_NAME, _COREDNS_NAMESPACE)
        except ApiException as exc:
            return False, (f"❌ Could not read CoreDNS deployment: {exc}")

        ready_replicas = dep.status.ready_replicas or 0
        desired_replicas = dep.spec.replicas or 0

        if ready_replicas < 1:
            return False, (f"❌ CoreDNS has {ready_replicas}/{desired_replicas} ready replicas.")

        # Check for CrashLoopBackOff on CoreDNS pods.
        crashloop_detected = False
        try:
            pods = self.core_v1.list_namespaced_pod(
                namespace=_COREDNS_NAMESPACE,
                label_selector="k8s-app=kube-dns",
            )
            for pod in pods.items:
                for cs in pod.status.container_statuses or []:
                    if cs.state and cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff":
                        crashloop_detected = True
                        break
                if crashloop_detected:
                    break
        except ApiException:
            pass

        if crashloop_detected:
            return False, (f"❌ CoreDNS has pods in CrashLoopBackOff ({ready_replicas}/{desired_replicas} ready).")

        return True, (
            f"✅ CoreDNS healthy: {ready_replicas}/{desired_replicas} replicas ready in {_COREDNS_NAMESPACE}."
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    @staticmethod
    def _fail(reason: str) -> dict:
        print(f"❌ {reason}")
        return {"success": False, "reason": reason}
