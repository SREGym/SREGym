"""SREGym problems ported to Slack Spine (helper slack_c).

Each class keeps the original fault's causal mechanism and its state-based
mitigation check, re-targeted at a Slack Spine component, and is wrapped with
the load generator health oracle by :func:`ported`. The load generator's
session mix is dominated by history reads and sends on svc-message (a send
checks the session on svc-auth and channel authz on svc-channel, which checks
the org policy on svc-workspace), then unread counts on svc-notification.
"""

from __future__ import annotations

import json
import shlex
import time

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from sregym.conductor.oracles.admission_webhook_outage_mitigation import AdmissionWebhookOutageMitigationOracle
from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.calico_route_reflector_mitigation import CalicoRouteReflectorMitigationOracle
from sregym.conductor.oracles.cumulative_admission_webhook_timeout_mitigation import (
    CumulativeAdmissionWebhookTimeoutMitigationOracle,
)
from sregym.conductor.oracles.dns_resolution_mitigation import DNSResolutionMitigationOracle
from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.ingress_misroute_oracle import IngressMisrouteMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.admission_webhook_tls_mismatch import AdmissionWebhookTLSMismatch
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.calico_route_reflector_label_drift import (
    CalicoRouteReflectorLabelDriftHotelReservation,
)
from sregym.conductor.problems.cumulative_admission_webhook_timeout_hotel_reservation import (
    CumulativeAdmissionWebhookTimeoutHotelReservation,
)
from sregym.conductor.problems.ingress_misroute import IngressMisroute
from sregym.conductor.problems.incident_arena.slack_spine import patch_role_db_config
from sregym.conductor.problems.lite_ia.k8s import ported
from sregym.conductor.problems.lite_ia.retry_storm import (
    DEFAULT_MESH,
    RetryStormCollapseIA,
    RetryStormMitigationOracle,
    _prom_sum,
)
from sregym.conductor.problems.pod_cidr_exhaustion_hotel_reservation import PodCIDRExhaustionHotelReservation
from sregym.conductor.problems.stale_coredns_config import StaleCoreDNSConfig
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.observer.ingress_nginx import IngressNginx
from sregym.service.rollout import deployment_rollout_complete
from sregym.utils.decorators import mark_fault_injected

APP = "slack_spine"


# ---------------------------------------------------------------------- helpers
def selector_of(problem, deployment: str) -> str:
    """Label selector built from a Deployment's ``spec.selector.matchLabels``."""
    dep = problem.kubectl.get_deployment(deployment, problem.namespace)
    return ",".join(f"{k}={v}" for k, v in sorted(dep.spec.selector.match_labels.items()))


def delete_pods(problem, deployment: str) -> list[str]:
    """Delete every pod of ``deployment`` so its ReplicaSet must create new ones under the fault."""
    pods = problem.kubectl.core_v1_api.list_namespaced_pod(
        problem.namespace, label_selector=selector_of(problem, deployment)
    ).items
    names = [pod.metadata.name for pod in pods]
    for name in names:
        try:
            problem.kubectl.core_v1_api.delete_namespaced_pod(
                name, problem.namespace, body=client.V1DeleteOptions(grace_period_seconds=0)
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
    print(f"Deleted {deployment} pods {names}")
    return names


def wait_rollout(problem, deployment: str, timeout_s: int = 600, check: bool = False) -> bool:
    """Wait until ``deployment`` is fully rolled out and Ready; returns whether it did."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if deployment_rollout_complete(problem.kubectl.get_deployment(deployment, problem.namespace)):
                return True
        except ApiException:
            pass
        time.sleep(5)
    if check:
        raise RuntimeError(f"deployment {deployment} did not become Ready within {timeout_s}s")
    print(f"[warn] deployment {deployment} not Ready after {timeout_s}s")
    return False


# Runs in the ops-toolbox: [[key, url, method, body], ...] on stdin, issued
# concurrently; prints {key: [status, body, elapsed_s]}.
_FETCH_PARALLEL = """
import json, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
def one(item):
    key, url, method, body = item
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return key, [resp.status, resp.read().decode("utf-8", "replace"), time.monotonic() - start]
    except urllib.error.HTTPError as exc:
        return key, [exc.code, exc.read().decode("utf-8", "replace"), time.monotonic() - start]
    except Exception as exc:
        return key, [0, repr(exc), time.monotonic() - start]
items = json.load(sys.stdin)
with ThreadPoolExecutor(max_workers=max(1, min(32, len(items)))) as pool:
    print(json.dumps(dict(pool.map(one, items))))
"""


def fetch_parallel(problem, requests: list[tuple[str, str, str, object]], timeout: float = 120) -> dict:
    """Issue HTTP requests concurrently from the ops toolbox; {key: [status, body, seconds]}."""
    out = problem.app.toolbox_exec(
        "python3 -c " + shlex.quote(_FETCH_PARALLEL),
        input_data=json.dumps([list(item) for item in requests]),
        timeout=timeout,
    )
    return json.loads(out.strip().splitlines()[-1])


def role_pod_ips(problem, role: str) -> dict[str, str]:
    pods = problem.kubectl.core_v1_api.list_namespaced_pod(
        problem.namespace, label_selector=problem.app.role_selector(role)
    ).items
    return {
        pod.metadata.name: pod.status.pod_ip
        for pod in pods
        if pod.metadata.deletion_timestamp is None and pod.status.phase == "Running" and pod.status.pod_ip
    }


# ---------------------------------------------------------------------- stale_coredns_config
class StaleCoreDNSConfigSlack(StaleCoreDNSConfig):
    """Cluster-wide NXDOMAIN template for ``svc.cluster.local`` in CoreDNS.

    Established connections survive the change, so svc-message (the users'
    entry point for history and sends) is replaced under the fault: its new
    pod cannot resolve ``db`` and never starts serving, and new client
    connections to every Service fail at name resolution.
    """

    def __init__(self, faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component="configmap/coredns",
            description=(
                "The CoreDNS Corefile (ConfigMap `coredns` in `kube-system`) carries a stale `template ANY ANY "
                'svc.cluster.local` block that answers NXDOMAIN for every name matching `.*\\.svc\\.cluster\\.local`, '
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


# ---------------------------------------------------------------------- admission_webhook_tls_mismatch
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
        ported(
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


# ---------------------------------------------------------------------- cumulative_admission_webhook_timeout
_EXTRA_TRUSTED_PREFIXES = ('    "postgres:",', '    "redpandadata/",', '    "minio/",')


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
        ported(
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


# ---------------------------------------------------------------------- pod_cidr_exhaustion
class PodCIDRExhaustionSlack(PodCIDRExhaustionHotelReservation):
    """Calico IPAM exhaustion: the default pool disabled, a tiny strict-affinity pool consumed.

    Only svc-message's pod is replaced (the original deleted every app pod):
    its replacement stays in ContainerCreating without an IP, so history reads
    and sends fail while the rest of Slack Spine keeps its addresses.
    """

    # Enough pause pods that the worker owning the tiny pool's only /26 block
    # fills it whatever the scheduler's spread (3 workers x ~70 pods).
    NUM_EXHAUST_PODS = 210
    BLOCK_SIZE = 64

    def __init__(self, faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        ported(
            self,
            APP,
            component="cluster-networking",
            description=(
                "Calico IPAM IP exhaustion. The default IPPool `default-ipv4-ippool` is disabled, the only enabled "
                f"pool is the tiny `{self.TINY_POOL_NAME}` ({self.TINY_POOL_CIDR}, one /26 block) and "
                "`strictAffinity` is enabled in IPAMConfig `default`, so nodes cannot borrow blocks from each other. "
                f"The Deployment `{self.EXHAUST_DEPLOYMENT}` in namespace `{self.EXHAUST_NAMESPACE}` "
                f"({self.NUM_EXHAUST_PODS} pause pods) has consumed every address of that block. The `{faulty_service}` "
                "pod was rescheduled and its replacement is stuck in ContainerCreating with `failed to request IPv4 "
                "addresses: Assigned 0 out of 1 requested IPv4 addresses; No more free affine blocks and strict "
                "affinity enabled`, so message history and sends fail although the Deployment spec is healthy. "
                "Mitigation: free IP allocations by scaling down the consuming workload, or restore the default IP "
                "pool (re-enable it / disable strictAffinity)."
            ),
            oracle_factory=MitigationOracle,
        )

    def _running_exhaust_pods(self) -> int:
        out = self.kubectl.exec_command(f"kubectl get pods -n {self.EXHAUST_NAMESPACE} --no-headers") or ""
        return sum(1 for line in out.splitlines() if " Running " in line)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.kubectl.exec_command(
            'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":true}}\''
        )
        self._apply_manifest(f"""apiVersion: crd.projectcalico.org/v1
kind: IPPool
metadata:
  name: {self.TINY_POOL_NAME}
spec:
  cidr: {self.TINY_POOL_CIDR}
  ipipMode: Always
  natOutgoing: true
  disabled: false
""")
        self.kubectl.exec_command(
            f'kubectl patch ippool {self.DEFAULT_POOL_NAME} --type=merge -p \'{{"spec":{{"disabled":true}}}}\''
        )
        self.kubectl.exec_command(
            f"kubectl create namespace {self.EXHAUST_NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -"
        )
        self._apply_manifest(f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {self.EXHAUST_DEPLOYMENT}
  namespace: {self.EXHAUST_NAMESPACE}
spec:
  replicas: {self.NUM_EXHAUST_PODS}
  selector:
    matchLabels:
      app: batch-worker
  template:
    metadata:
      labels:
        app: batch-worker
        workload-type: batch
        priority: low
        team: data-engineering
    spec:
      containers:
      - name: worker
        image: registry.k8s.io/pause:3.9
        resources:
          requests:
            cpu: "1m"
            memory: "1Mi"
""")
        # The block is full once a block's worth of pause pods run (the
        # others wait without an address).
        deadline = time.monotonic() + 300
        running = 0
        while time.monotonic() < deadline:
            running = self._running_exhaust_pods()
            if running >= self.BLOCK_SIZE - 2:
                break
            time.sleep(5)
        print(f"{running} pause pods hold addresses from {self.TINY_POOL_NAME}")
        time.sleep(10)
        delete_pods(self, self.faulty_service)
        print(f"IP pool exhausted; {self.faulty_service} cannot get a pod IP")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.kubectl.exec_command(
            f"kubectl scale deployment {self.EXHAUST_DEPLOYMENT} -n {self.EXHAUST_NAMESPACE} --replicas=0"
        )
        self.kubectl.exec_command(
            f'kubectl patch ippool {self.DEFAULT_POOL_NAME} --type=merge -p \'{{"spec":{{"disabled":false}}}}\''
        )
        self.kubectl.exec_command(f"kubectl delete ippool {self.TINY_POOL_NAME} --ignore-not-found")
        self.kubectl.exec_command(
            'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":false}}\''
        )
        self.kubectl.exec_command(f"kubectl delete namespace {self.EXHAUST_NAMESPACE} --ignore-not-found --wait=false")
        # Pods stuck without an address back off between CNI retries; replace them.
        out = self.kubectl.exec_command(f"kubectl get pods -n {self.namespace} --no-headers") or ""
        for line in out.splitlines():
            if "ContainerCreating" in line:
                self.kubectl.exec_command(f"kubectl delete pod {line.split()[0]} -n {self.namespace} --wait=false")
        wait_rollout(self, self.faulty_service, timeout_s=300)
        print("Recovery complete")



# ---------------------------------------------------------------------- feature_flag_latent_bug
class SequencerModeOracle(Oracle):
    """The latent allocator is off (persisted and live) and concurrent sends get distinct sequence numbers.

    The probe plays the original's HTTP probe: the dormant path only shows
    when requests arrive, so it fires a burst of concurrent sends into one
    fresh channel and requires every send to succeed with a unique ``seq``.
    """

    importance = 1.0
    PROBE_SENDS = 24
    FAILURE_CLASSES = {
        "fault_still_present": FailureClass.AGENT_ERROR,
        "sends_failing": FailureClass.AGENT_ERROR,
        "required_deployment_not_rolled_out": FailureClass.AGENT_ERROR,
        "probe_unavailable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Sequencer mode mitigation check ==")
        p = self.problem
        try:
            if not wait_rollout(p, p.faulty_service, timeout_s=180):
                return self.fail("required_deployment_not_rolled_out", deployment=p.faulty_service)
            dep = p.kubectl.get_deployment(p.faulty_service, p.namespace)
            env = {e.name: e.value for c in dep.spec.template.spec.containers for e in (c.env or [])}
            persisted = env.get(p.flag_key)
            if persisted not in (None, "", "atomic"):
                print(f"❌ Deployment {p.faulty_service} still sets {p.flag_key}={persisted}")
                return self.fail("fault_still_present", persisted=persisted)
            pods = role_pod_ips(p, "message")
            live = fetch_parallel(p, [(n, f"http://{ip}:8000/admin/sequencer", "GET", None) for n, ip in pods.items()])
            for pod, (status, body, _) in live.items():
                mode = json.loads(body).get("mode") if status == 200 else None
                print(f"[live] {pod} sequencer mode={mode}")
                if mode != "atomic":
                    return self.fail("fault_still_present", pod=pod, live_mode=mode)
            channel = f"seq-probe-{time.time_ns()}"
            sends = [
                (f"send-{i}", "http://svc-message:8000/messages", "POST",
                 {"channel_id": channel, "client_msg_id": f"{channel}-{i}", "text": f"probe {i}"})
                for i in range(self.PROBE_SENDS)
            ]
            replies = fetch_parallel(p, sends)
        except Exception as exc:
            print(f"❌ Probe failed: {exc}")
            return self.fail("probe_unavailable", error=str(exc))
        statuses = [int(status) for status, _, _ in replies.values()]
        seqs = [json.loads(body).get("seq") for status, body, _ in replies.values() if 200 <= int(status) < 300]
        print(f"[probe] {len(seqs)}/{self.PROBE_SENDS} sends ok, {len(set(seqs))} distinct seqs")
        if len(seqs) < self.PROBE_SENDS:
            return self.fail("sends_failing", statuses=sorted(statuses))
        if len(set(seqs)) != len(seqs) or sorted(seqs) != list(range(1, self.PROBE_SENDS + 1)):
            return self.fail("fault_still_present", seqs=sorted(seqs))
        print("✅ Atomic sequencer: concurrent sends got dense, unique sequence numbers")
        return {"success": True}


class FeatureFlagLatentBugSlack(Problem):
    """Analogue: a config flag (``SEQUENCER_MODE=rmw``) activates a latent code path in svc-message.

    The original flips ``SEARCH_BACKEND_VERSION`` and the frontend image's
    dormant path fails every search. Slack Spine's image ships a dormant
    non-atomic read-modify-write channel_seq allocator behind
    ``SEQUENCER_MODE``; with the flag on and concurrent sends arriving it
    hands out duplicate sequence numbers while every pod stays Running and
    every send returns 201. (Incident Arena task 007 uses the same toggle.)
    """

    def __init__(self, faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        self.flag_key = "SEQUENCER_MODE"
        ported(
            self,
            APP,
            component=f"deployment/{faulty_service}",
            description=(
                f"A configuration change set `{self.flag_key}=rmw` on the `{faulty_service}` Deployment, activating "
                "a dormant code path compiled into the message image: instead of the atomic `INSERT ... ON CONFLICT "
                "DO UPDATE ... RETURNING` channel_seq increment, sends read `channel_seq.last_seq`, hold the "
                "transaction, and write `last_seq + 1` back (a non-atomic read-modify-write). With the flag on and "
                "concurrent sends to the same channel, sends read the same cursor and persist duplicate per-channel "
                "sequence numbers, so channel history order is silently corrupted. Every pod stays Running and "
                "sends still return 201, so the failure is visible only in the data (duplicate `messages.seq` per "
                "channel) and in svc-message GET /admin/sequencer reporting `rmw`. The fix is to revert the flag "
                "(unset it or set `atomic`) on the Deployment and roll svc-message out so the atomic allocator is "
                "live again."
            ),
            oracle_factory=SequencerModeOracle,
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} -c app {self.flag_key}=rmw"
        )
        wait_rollout(self, self.faulty_service, timeout_s=300, check=True)
        print(f"{self.flag_key}=rmw on {self.faulty_service}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.kubectl.exec_command_checked(
            f"kubectl set env deployment/{self.faulty_service} -n {self.namespace} -c app {self.flag_key}-"
        )
        wait_rollout(self, self.faulty_service, timeout_s=300)


# ---------------------------------------------------------------------- astronomy_shop_ad_service_image_slow_load
class StoreStrictEventOracle(Oracle):
    """The runtime toggle is off on every pod of the role and its user endpoint answers promptly."""

    importance = 1.0
    PROBES = 5
    MAX_LATENCY_S = 2.0
    FAILURE_CLASSES = {
        "fault_still_present": FailureClass.AGENT_ERROR,
        "endpoint_slow_or_failing": FailureClass.AGENT_ERROR,
        "required_deployment_not_rolled_out": FailureClass.AGENT_ERROR,
        "probe_unavailable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Runtime toggle mitigation check ==")
        p = self.problem
        try:
            if not wait_rollout(p, p.faulty_service, timeout_s=180):
                return self.fail("required_deployment_not_rolled_out", deployment=p.faulty_service)
            pods = role_pod_ips(p, p.role)
            events = fetch_parallel(p, [(n, f"http://{ip}:8000/admin/event", "GET", None) for n, ip in pods.items()])
            for pod, (status, body, _) in events.items():
                active = json.loads(body).get("active", []) if int(status) == 200 else None
                print(f"[live] {pod} active events={active}")
                if active is None or p.event in active:
                    return self.fail("fault_still_present", pod=pod, active=active)
            # One at a time: a concurrent burst would mostly measure the toolbox's own CPU throttling.
            probes = {}
            for i in range(self.PROBES):
                url = f"http://{p.faulty_service}:8000/unread?user_id=probe-{i}&channel_id=probe"
                probes.update(fetch_parallel(p, [(f"unread-{i}", url, "GET", None)]))
        except Exception as exc:
            print(f"❌ Probe failed: {exc}")
            return self.fail("probe_unavailable", error=str(exc))
        slow = {k: (s, round(t, 2)) for k, (s, _, t) in probes.items() if int(s) != 200 or t > self.MAX_LATENCY_S}
        print(f"[probe] GET /unread: {len(probes) - len(slow)}/{len(probes)} fast and OK")
        if slow:
            return self.fail("endpoint_slow_or_failing", slow=slow)
        print("✅ Toggle cleared and /unread is fast")
        return {"success": True}


class ImageSlowLoadSlack(Problem):
    """Analogue: a runtime flag injects a fixed 10s delay into one user-facing read path.

    The original flips flagd's ``imageSlowLoad`` to its ``10sec`` variant and
    Envoy delays every product image by 10s. Slack Spine's servicekit has the
    same kind of runtime toggle: ``store_consistency_strict`` (``PUT
    /admin/event``) makes each store operation of a role wait
    ``STORE_HOLD_MS``. Here the dose is 10s (a latent deploy-time env) and the
    toggle is pushed only onto svc-notification, so users' unread badges
    (``GET /unread``) take 10s and time out while everything else is fast.
    (Incident Arena tasks 009/010 push the same event onto auth, workspace and
    notification together with a 100-250 ms dose.)
    """

    HOLD_MS = "10000"

    def __init__(self, role: str = "notification"):
        self.role = role
        self.faulty_service = f"svc-{role}"
        self.event = "store_consistency_strict"
        ported(
            self,
            APP,
            component=self.faulty_service,
            description=(
                f"The `{self.faulty_service}` deployment serves unread counts (`GET /unread`, the users' unread "
                "badges) with abnormally high latency: every request takes ~10 s, so clients time out while every "
                "other endpoint is fast and every pod is Running and Ready. Mechanism: the runtime consistency "
                f"toggle `{self.event}` was switched on for svc-{role} via `PUT /admin/event` "
                f'(`{{"name": "{self.event}", "active": true}}`; `GET /admin/event` lists it as active). While it '
                f"is active every Redis store operation of the role first waits `STORE_HOLD_MS`, which this "
                f"deployment sets to {self.HOLD_MS} ms. Mitigation: deactivate the event on the role (`PUT "
                f'/admin/event {{"name": "{self.event}", "active": false}}`), or otherwise stop the delayed path.'
            ),
            oracle_factory=StoreStrictEventOracle,
        )
        self.app.configure({"app": {"roles": {role: {"env": {"STORE_HOLD_MS": self.HOLD_MS}}}}})

    def _set(self, active: bool) -> None:
        pods = role_pod_ips(self, self.role)
        replies = fetch_parallel(
            self,
            [(n, f"http://{ip}:8000/admin/event", "PUT", {"name": self.event, "active": active}) for n, ip in pods.items()],
        )
        for pod, (status, body, _) in replies.items():
            if int(status) != 200:
                raise RuntimeError(f"PUT /admin/event on {pod} returned {status}: {body[:200]}")
            print(f"{pod}: active events {json.loads(body).get('active')}")

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._set(True)

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._set(False)


# ---------------------------------------------------------------------- calico_route_reflector_label_drift
class CalicoRouteReflectorLabelDriftSlack(CalicoRouteReflectorLabelDriftHotelReservation):
    """Calico route reflector selected by the deprecated master label, which an upgrade removed.

    svc-message and svc-auth are pinned to different workers first (the
    original pinned frontend and reservation), so the send path's session
    check crosses nodes; with node-to-node mesh off and no reflector
    selected, cross-node pod traffic stops.
    """

    CROSS_NODE_PAIR = ("svc-message", "svc-auth")

    def __init__(self):
        self.faulty_service = None
        self.frontend_probe_path = "/healthz"
        self.route_reflector_node = None
        self.worker_nodes = []
        self.original_bgp_configuration = None
        self._original_bgppeer_names = None
        self._bgp_config_preexisted = None
        self._legacy_label_preexisted = None
        self._route_reflector_annotation_preexisted = None
        self._route_reflector_annotation_value = None
        self._app_deployment_replicas = {}
        ported(
            self,
            APP,
            component=f"BGPPeer/{self.BGP_PEER_NAME}",
            description=(
                "Calico runs in route-reflector mode with node-to-node mesh disabled (BGPConfiguration `default`, "
                f"`nodeToNodeMeshEnabled: false`). The BGPPeer `{self.BGP_PEER_NAME}` selects route reflectors with "
                f"the deprecated `{self.LEGACY_MASTER_LABEL}` label, but after a label migration the intended "
                f"control-plane route-reflector node only carries `{self.CURRENT_CONTROL_PLANE_LABEL}`. No node is "
                "selected as route reflector, so Calico stops propagating routes between nodes and cross-node pod "
                "and Service traffic is dropped. Slack Spine's pods stay mostly Running, but every cross-node hop "
                "fails: svc-message (pinned to one worker) cannot reach svc-auth (pinned to another), the database "
                "or its other dependencies on other nodes, and clients on other nodes cannot reach svc-message, so "
                "history reads and sends fail or time out. Mitigation must repair the BGPPeer selector (or "
                "intentionally restore the expected route-reflector label) while preserving the route-reflector "
                "topology (node-to-node mesh stays disabled)."
            ),
            oracle_factory=CalicoRouteReflectorMitigationOracle,
        )
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self._app_cleanup = self.app.cleanup
        self.app.cleanup = self._cleanup

    def _prepare_cross_node_app_path(self):
        for deployment, node in zip(self.CROSS_NODE_PAIR, self.worker_nodes, strict=False):
            self._pin_deployment_to_node(deployment, node)

    def _restore_app_scheduling(self):
        for deployment in self.CROSS_NODE_PAIR:
            self._unpin_deployment_from_node(deployment)


# ---------------------------------------------------------------------- stale_hostaliases_dns_poisoning
# The chart's default session mix with posts weighted up (CreatePost 1.0 -> 6.0):
# a send-heavy user population, so failing sends dominate what users see.
SEND_HEAVY_ACTION_WEIGHTS = {
    "session_login": 0.12,
    "session_history": 9.79,
    "session_unread": 1.32,
    "session_post": 6.0,
    "session_thread": 0.88,
    "session_reply": 0.18,
    "session_reaction": 0.13,
    "session_edit": 0.04,
    "session_delete": 0.02,
    "session_file_upload": 0.08,
    "session_file_download": 0.08,
    "session_presence": 0.144,
    "session_search": 0.054,
}


class StaleHostAliasesOracle(Oracle):
    """Routing converged: no stale resolver entry, the rollout finished, every serving pod sends successfully.

    The fix must not depend on the retired entrypoint (it stays withdrawn)
    and must keep the edge's serving capacity (at least ``EDGE_REPLICAS``).
    """

    importance = 1.0
    FAILURE_CLASSES = {
        "fault_still_present": FailureClass.AGENT_ERROR,
        "rollout_paused_or_incomplete": FailureClass.AGENT_ERROR,
        "serving_capacity_reduced": FailureClass.AGENT_ERROR,
        "sends_failing": FailureClass.AGENT_ERROR,
        "probe_unavailable": FailureClass.AMBIGUOUS,
    }

    def evaluate(self, *args, **kwargs) -> dict:
        print("== Stale hostAliases mitigation check ==")
        p = self.problem
        try:
            dep = p.kubectl.get_deployment(p.faulty_service, p.namespace)
            if dep.spec.paused:
                print(f"❌ Deployment {p.faulty_service} rollout is still paused")
                return self.fail("rollout_paused_or_incomplete", paused=True)
            if (dep.spec.replicas or 0) < p.EDGE_REPLICAS:
                print(f"❌ {p.faulty_service} scaled to {dep.spec.replicas} (< {p.EDGE_REPLICAS})")
                return self.fail("serving_capacity_reduced", replicas=dep.spec.replicas)
            if not wait_rollout(p, p.faulty_service, timeout_s=180):
                return self.fail("rollout_paused_or_incomplete", paused=False)
            pods = [
                pod
                for pod in p.kubectl.core_v1_api.list_namespaced_pod(
                    p.namespace, label_selector=selector_of(p, p.faulty_service)
                ).items
                if pod.metadata.deletion_timestamp is None
            ]
            for pod in pods:
                for alias in pod.spec.host_aliases or []:
                    if p.TARGET_BACKEND in (alias.hostnames or []):
                        print(f"❌ Pod {pod.metadata.name} still pins {p.TARGET_BACKEND} to {alias.ip}")
                        return self.fail("fault_still_present", pod=pod.metadata.name, alias_ip=alias.ip)
            ready = {pod.metadata.name: pod.status.pod_ip for pod in pods if pod.status.pod_ip}
            if len(ready) < p.EDGE_REPLICAS:
                return self.fail("serving_capacity_reduced", pods=sorted(ready))
            stamp = time.time_ns()
            sends = [
                (f"{name}#{i}", f"http://{ip}:8000/messages", "POST",
                 {"channel_id": f"alias-probe-{i}", "client_msg_id": f"alias-probe-{stamp}-{name}-{i}", "text": "probe"})
                for name, ip in sorted(ready.items())
                for i in range(3)
            ]
            replies = fetch_parallel(p, sends)
        except Exception as exc:
            print(f"❌ Probe failed: {exc}")
            return self.fail("probe_unavailable", error=str(exc))
        failed = {key: (status, body[:120]) for key, (status, body, _) in replies.items() if not 200 <= int(status) < 300}
        print(f"[probe] per-pod sends: {len(replies) - len(failed)}/{len(replies)} ok")
        if failed:
            return self.fail("sends_failing", failed=failed)
        print("✅ Every serving svc-message pod resolves svc-auth through the current Service")
        return {"success": True}


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
    load_profile = ("lite_slack_sendheavy", {"base": "slack_session", "soak_cycles": 2, "action_weights": SEND_HEAVY_ACTION_WEIGHTS})

    def __init__(self, faulty_service: str = "svc-message"):
        self.faulty_service = faulty_service
        self.saved: dict | None = None
        ported(
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
        self.kubectl.exec_command_checked(
            f"kubectl apply -n {self.namespace} -f -", input_data=json.dumps(legacy)
        )
        legacy_ip = self.kubectl.core_v1_api.read_namespaced_service(self.LEGACY_SERVICE, self.namespace).spec.cluster_ip
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
        self.kubectl.exec_command(f"kubectl delete service {self.LEGACY_SERVICE} -n {self.namespace} --ignore-not-found")


# ---------------------------------------------------------------------- ingress_misroute
class IngressMisrouteSlack(IngressMisroute):
    """An ingress-nginx Ingress in front of svc-message has its ``/api`` rule pointed at svc-thread.

    The load generator's message traffic (history reads, posts, edits,
    reactions) reaches svc-message through the ingress controller (deploy
    time: ``loadgen.target`` is the controller's ``/api`` prefix); the other
    roles it calls directly. The fault repoints the path's backend.
    """

    CONTROLLER_URL = "http://ingress-nginx-controller.ingress-nginx.svc.cluster.local"
    PATH = "/api"

    def __init__(self, correct_service: str = "svc-message", wrong_service: str = "svc-thread"):
        self.path = self.PATH
        self.correct_service = correct_service
        self.wrong_service = wrong_service
        self.ingress_name = "slack-spine-ingress"
        self.faulty_service = [correct_service, wrong_service]
        ported(
            self,
            APP,
            component=self.ingress_name,
            description=(
                f"Ingress `{self.ingress_name}` (ingress-nginx, the edge that serves the users' message API under "
                f"`{self.path}`) has a misconfigured backend rule for path `{self.path}(/|$)(.*)`: it routes "
                f"requests to Service `{wrong_service}` instead of `{correct_service}`. Traffic reaches a valid, "
                f"healthy service, but `{wrong_service}` does not serve the message API, so history reads "
                "(`GET /channels/:id/messages`), sends (`POST /messages`), edits and reactions return 404 through "
                "the edge, while direct calls to svc-message and every pod stay healthy. Mitigation: point the "
                f"`{self.path}` rule back at `{correct_service}`."
            ),
            oracle_factory=IngressMisrouteMitigationOracle,
        )
        self.networking_v1 = client.NetworkingV1Api()
        base = {f"LOADGEN_{key}_BASE_URL": f"http://svc-{role}:8000" for key, role in
                (("THREAD", "thread"), ("NOTIF", "notification"), ("SEARCH", "search"))}
        self.app.configure(
            {
                "loadgen": {"target": self.CONTROLLER_URL + self.path},
                "sregym": {
                    "loadgenEnv": [
                        {"name": "EPISODE_START_TIMEOUT_S", "value": "86400"},
                        *({"name": k, "value": v} for k, v in base.items()),
                    ]
                },
            }
        )
        # The edge must exist before the load generator starts its episode.
        self._app_deploy = self.app.deploy
        self.app.deploy = self._deploy_with_edge

    def _ingress(self, backend: str) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": self.ingress_name,
                "namespace": self.namespace,
                "annotations": {"nginx.ingress.kubernetes.io/rewrite-target": "/$2"},
            },
            "spec": {
                "ingressClassName": "nginx",
                "rules": [
                    {
                        "http": {
                            "paths": [
                                {
                                    "path": self.path + "(/|$)(.*)",
                                    "pathType": "ImplementationSpecific",
                                    "backend": {"service": {"name": backend, "port": {"number": 8000}}},
                                }
                            ]
                        }
                    }
                ],
            },
        }

    def _install_edge(self) -> None:
        IngressNginx().deploy()
        self.kubectl.create_namespace_if_not_exist(self.namespace)
        self.kubectl.exec_command_checked(
            f"kubectl apply -n {self.namespace} -f -", input_data=json.dumps(self._ingress(self.correct_service))
        )

    def _ensure_edge(self) -> None:
        self._install_edge()
        # Wait until the controller serves the route.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status, _ = self.app.http(f"{self.CONTROLLER_URL}{self.path}/healthz", timeout=5)
            if status == 200:
                return
            time.sleep(3)
        raise RuntimeError("ingress-nginx did not route /api/healthz to svc-message")

    def _deploy_with_edge(self):
        # The load generator's init container waits for its target, so the
        # controller and the Ingress exist before the chart is installed.
        self._install_edge()
        self._app_deploy()
        self._ensure_edge()

    def _set_backend(self, backend: str) -> None:
        ingress = self.networking_v1.read_namespaced_ingress(name=self.ingress_name, namespace=self.namespace)
        for rule in ingress.spec.rules:
            for path in rule.http.paths:
                if path.path.startswith(self.path):
                    path.backend.service.name = backend
        self.networking_v1.replace_namespaced_ingress(name=self.ingress_name, namespace=self.namespace, body=ingress)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        try:
            self.networking_v1.read_namespaced_ingress(name=self.ingress_name, namespace=self.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self._ensure_edge()
        self._set_backend(self.wrong_service)
        print(f"Ingress {self.ingress_name}: {self.path} -> {self.wrong_service}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._set_backend(self.correct_service)


# ---------------------------------------------------------------------- rpc retry storms (message -> channel)
_SPIKE_SCRIPT = """
import json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
RPS, DUR = {rps}, {seconds}
def send(i):
    body = json.dumps({{'channel_id': f'spike-{{i % 64}}', 'client_msg_id': f'spike-{{time.time_ns()}}-{{i}}', 'text': 'spike'}}).encode()
    req = urllib.request.Request('http://svc-message:8000/messages', data=body, method='POST', headers={{'Content-Type': 'application/json'}})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass
pool = ThreadPoolExecutor(max_workers=256)
start = time.monotonic(); i = 0
while time.monotonic() - start < DUR:
    pool.submit(send, i); i += 1
    time.sleep(max(0.0, start + i / RPS - time.monotonic()))
pool.shutdown(wait=False, cancel_futures=True)
print('spike sent', i)
"""


class ChannelRetryStormBase(RetryStormCollapseIA):
    """Metastable retry storm on svc-message's channel authz call (message -> channel).

    Ports of the BlueprintHotelReservation ``rpc`` retry storms (an RPC
    config with a very low timeout and many retries; a trigger tips the
    system; retries keep demand above the backend's capacity afterwards).
    The Lite port already uses channel -> workspace; these use the edge one
    hop up. Every send (``POST /messages``) resolves channel authz on
    svc-channel (``GET /authz/resolve``) through svc-message's mesh client
    (``roles.message.mesh``, deployed with ``caller_mesh``). svc-channel is the
    bounded backend: strict ACL reads (``ACTIVE_EVENTS=read_consistency_strict``,
    ``ACL_HOLD_MS=100``) hold a pooled Postgres connection per resolve, so
    the pool bounds its throughput; an abandoned attempt still waits for and
    holds a connection, so retries of timed-out attempts are pure extra load.
    """

    CALLER_ROLE = "message"
    BACKEND_ROLE = "channel"
    UPSTREAM_ROLE = "message"
    caller_mesh = {
        "retries": 3,
        "retryOnTimeout": True,
        "perTryTimeoutMs": 300,
        "backoffMs": 0,
        "breakerEnabled": False,
        "breakerThreshold": 1000000,
    }
    backend_env = {"ACTIVE_EVENTS": "read_consistency_strict", "ACL_HOLD_MS": "100"}
    backend_db = {"pool_size": 8, "max_overflow": 4}
    min_acl_hold_ms = 100
    # Every arrival is a real send (POST /messages, then index + search readback).
    load_profile = ("lite_slack_send", {"base": "write", "cycles": [[30.0, 30.0, 30.0, 30.0]], "soak_cycles": 2})

    def __init__(self, description: str, component: str):
        ported(self, APP, component=component, description=description, oracle_factory=ChannelRetryStormOracle)
        self.app.set_load_profile(*self.load_profile)
        self.app.configure(
            {
                "app": {
                    "roles": {
                        self.CALLER_ROLE: {"mesh": dict(self.caller_mesh)},
                        self.BACKEND_ROLE: {"db": dict(self.backend_db), "env": dict(self.backend_env)},
                    }
                }
            }
        )
        self._injection_attempted = False

    # ------------------------------------------------------------------ metrics
    def metrics(self) -> dict[str, float]:
        requests = self.role_requests(self.CALLER_ROLE, "/metrics") + self.role_requests(self.BACKEND_ROLE, "/metrics")
        replies = self.fetch_many(requests)
        snap = {k: 0.0 for k in ("authz_calls", "backend_attempts", "backend_timeouts", "backend_ok", "pool_checked_out", "pool_capacity")}
        for key, (status, text) in replies.items():
            if int(status) != 200:
                raise RuntimeError(f"GET {key} returned {status}: {text[:200]}")
            if key.split("/", 1)[0] == self.CALLER_ROLE:
                target = self.BACKEND_ROLE
                snap["authz_calls"] += _prom_sum(text, "http_request_duration_seconds_count", method="POST", route="/messages")
                snap["backend_attempts"] += _prom_sum(text, "http_client_attempts_total", target=target)
                snap["backend_timeouts"] += _prom_sum(text, "http_client_attempts_total", target=target, result="timeout")
                snap["backend_ok"] += _prom_sum(text, "http_client_attempts_total", target=target, result="ok")
            else:
                snap["pool_checked_out"] += _prom_sum(text, "db_pool_checked_out")
                snap["pool_capacity"] += _prom_sum(text, "db_pool_capacity")
        return snap

    @staticmethod
    def describe(sample: dict) -> str:
        return (
            f"loadgen offered={sample['offered']} error_rate={sample['error_rate']:.1%} "
            f"sends={sample['authz_calls']:.0f} channel_attempts={sample['backend_attempts']:.0f} "
            f"amplification={sample['amplification']:.2f} timeouts={sample['timeout_share']:.0%} "
            f"pool_in_use={sample['pool_checked_out']:.0f}/{sample['pool_capacity']:.0f}"
        )

    # ------------------------------------------------------------------ knobs
    def set_backend_pool(self, pool_size: int, max_overflow: int) -> None:
        """Resize svc-channel's pool live and persist it in app-config."""
        cm = self.kubectl.core_v1_api.read_namespaced_config_map(self.app.APP_CONFIG_MAP, self.namespace)
        text = patch_role_db_config(
            cm.data["app.yaml"], self.BACKEND_ROLE, {"pool_size": pool_size, "max_overflow": max_overflow}
        )
        self.kubectl.core_v1_api.patch_namespaced_config_map(
            self.app.APP_CONFIG_MAP, self.namespace, {"data": {"app.yaml": text}}
        )
        self.admin_all(self.BACKEND_ROLE, "/admin/config", "PUT", {"db": {"pool_size": pool_size, "max_overflow": max_overflow}})
        self.admin_all(self.BACKEND_ROLE, "/admin/reload", "POST")

    def set_trigger(self, active: bool) -> None:  # no runtime event of our own to clear
        return None

    def tip(self) -> None:
        raise NotImplementedError

    def _arm(self) -> None:
        return None

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        try:
            self.set_caller_mesh(self.caller_mesh)
            self._verify_healthy_baseline()
            self._arm()
            self.tip()
            latched, failures = self._storm_sustained()
            if not latched:
                print(f"[Trigger] Storm did not latch ({'; '.join(failures)}); repeating the trigger")
                self.tip()
                latched, failures = self._storm_sustained()
            if not latched:
                raise RuntimeError("metastable state was not established: " + "; ".join(failures))
        except Exception:
            try:
                self.set_caller_mesh(DEFAULT_MESH)
            except Exception as cleanup_error:
                print(f"[Cleanup] Failed to restore the safe mesh policy: {cleanup_error}")
            raise
        print(f"Fault: retry storm latched | svc-message -> svc-channel | Namespace: {self.namespace}\n")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self.set_caller_mesh(DEFAULT_MESH)
        self._injection_attempted = False
        print("Restored the default mesh policy on svc-message")


class ChannelRetryStormOracle(RetryStormMitigationOracle):
    """Safe svc-message mesh policy and svc-channel capacity, recovered sends, and survival of a replayed trigger."""

    def _policy_outside_envelope(self) -> dict | None:
        p = self.problem
        try:
            for pod, config in p.admin_all(p.CALLER_ROLE, "/admin/config").items():
                mesh = config.get("mesh") or {}
                retries = int(mesh.get("retries", 1))
                timeout_ms = float(mesh.get("perTryTimeoutMs", 0))
                backoff_ms = float(mesh.get("backoffMs", 0))
                breaker = bool(mesh.get("breakerEnabled", False))
                threshold = int(mesh.get("breakerThreshold", DEFAULT_MESH["breakerThreshold"]))
                print(f"[Policy] {pod} mesh={json.dumps(mesh, sort_keys=True)}")
                if (
                    retries > p.max_retries
                    or timeout_ms > p.max_per_try_timeout_ms
                    or backoff_ms > p.max_backoff_ms
                    or (breaker and threshold < p.min_breaker_threshold)
                ):
                    print(
                        f"[FAIL] {pod}: mesh policy outside the safe envelope (retries <= {p.max_retries}, "
                        f"perTryTimeoutMs <= {p.max_per_try_timeout_ms}, backoffMs <= {p.max_backoff_ms}, "
                        f"breakerThreshold >= {p.min_breaker_threshold} when enabled)"
                    )
                    return self.fail("retry_policy_outside_safe_envelope", pod=pod, mesh=mesh)
            connections = 0
            for pod, config in p.admin_all(p.BACKEND_ROLE, "/admin/config").items():
                db = config.get("db") or {}
                pool_size, overflow = int(db.get("pool_size", 0)), int(db.get("max_overflow", 0))
                pool_timeout = float(db.get("pool_timeout_s", 0))
                connections += pool_size + overflow
                print(f"[Policy] {pod} db={json.dumps(db, sort_keys=True)}")
                if (
                    not 0 < pool_size <= p.max_pool_size
                    or not 0 <= overflow <= p.max_overflow
                    or not p.min_pool_timeout_s <= pool_timeout <= p.max_pool_timeout_s
                ):
                    print(f"[FAIL] {pod}: pool outside the safe envelope")
                    return self.fail("backend_pool_outside_safe_envelope", pod=pod, db=db)
            if connections > p.max_backend_connections:
                return self.fail("backend_pool_outside_safe_envelope", connections=connections)
            message_env, _ = self._container_env(p.UPSTREAM_ROLE)
            if message_env.get("AUTHZ_CHECK") != "1":
                print("[FAIL] The send path no longer performs the channel authz check")
                return self.fail("policy_check_disabled")
            channel_env, _ = self._container_env(p.BACKEND_ROLE)
            strict_live = all(
                "read_consistency_strict" in (state.get("active") or [])
                for state in p.admin_all(p.BACKEND_ROLE, "/admin/event").values()
            )
            hold = float(channel_env.get("ACL_HOLD_MS", 250))
            if not strict_live or hold < p.min_acl_hold_ms:
                print("[FAIL] Strict ACL reads were turned off or made cheaper instead of fixing the feedback loop")
                return self.fail("trigger_neutralized", strict=strict_live, acl_hold_ms=hold)
        except ApiException as exc:
            return self.fail_from_exception(exc)
        except Exception as exc:
            print(f"[FAIL] The effective policy could not be read: {exc}")
            return self.fail("metrics_unreadable", error=f"{type(exc).__name__}: {exc}")
        return None

    def evaluate(self, *args, **kwargs) -> dict:
        # The replay is the problem's own trigger.
        problem = self.problem
        problem._trigger = lambda _seconds: problem.tip()
        try:
            return super().evaluate(*args, **kwargs)
        finally:
            del problem._trigger


class CapacityDecreaseRPCRetryStormSlack(ChannelRetryStormBase):
    """Trigger: a permanent cut of svc-channel's pool plus a transient latency pulse.

    The original injects a transient latency/CPU-stress trigger and applies a
    permanent capacity restraint. Here svc-channel's pool is cut from 8+4 to
    ``CUT_POOL`` (persisted in app-config) and a 10 s ``org_policy_revalidate``
    event on svc-workspace adds ~250 ms to every resolve (the org-policy check
    precedes the ACL read), pushing it past the 300 ms per-try timeout.
    """

    CUT_POOL = (6, 1)
    TRIGGER_EVENT = "org_policy_revalidate"
    TRIGGER_ROLE = "workspace"

    def __init__(self):
        mesh = self.caller_mesh
        super().__init__(
            component="svc-message -> svc-channel mesh retry policy (roles.message.mesh)",
            description=(
                "A metastable retry storm on the send path after a capacity decrease. Every message send "
                "(svc-message POST /messages) resolves channel authz on svc-channel (GET /authz/resolve) through "
                "svc-message's mesh client, whose policy (`roles.message.mesh` in ConfigMap app-config, also returned "
                f"by svc-message GET /admin/config) is aggressive: {mesh['retries']} attempts, retryOnTimeout=true, "
                f"{mesh['perTryTimeoutMs']} ms per-try timeout, no backoff. svc-channel reads ACLs in strict mode "
                f"(ACTIVE_EVENTS=read_consistency_strict, ACL_HOLD_MS={self.backend_env['ACL_HOLD_MS']}): each "
                "resolve holds a pooled Postgres connection, so its pool bounds throughput. Its capacity was "
                f"permanently decreased: the pool was cut from {self.backend_db['pool_size']}+"
                f"{self.backend_db['max_overflow']} to {self.CUT_POOL[0]}+{self.CUT_POOL[1]} connections "
                "(`roles.channel.db` in app-config, live on svc-channel), still enough for normal traffic. A transient "
                "slowdown (a ~10 s `org_policy_revalidate` event on svc-workspace, already cleared) pushed resolves "
                "past the 300 ms per-try timeout; svc-message retried the timed-out calls while the abandoned attempts "
                "still waited for and held channel connections, so ~3 attempts per send keep demand above the reduced "
                "pool's capacity after the event ended: the pool stays saturated, every attempt times out and sends "
                "fail with 503 `authz_unavailable`. The sustaining cause is the timeout/retry/queue feedback loop "
                "(made self-sustaining by the capacity cut), not the expired event. Valid mitigations bring the retry "
                "policy (fewer attempts, a per-try timeout above the queueing delay, no retry on timeout, or a sane "
                "breaker) and/or svc-channel's pool (within the peer-uniform 20+10) into a safe envelope so the "
                "backlog drains and a repeated slowdown is survived; disabling strict ACL reads or the authz check is "
                "not a fix."
            ),
        )

    def _arm(self) -> None:
        print(f"[Capacity] svc-channel pool -> {self.CUT_POOL[0]}+{self.CUT_POOL[1]} (permanent)")
        self.set_backend_pool(*self.CUT_POOL)

    def _pulse(self, active: bool) -> None:
        self.admin_all(self.TRIGGER_ROLE, "/admin/event", "PUT", {"name": self.TRIGGER_EVENT, "active": active})

    def tip(self) -> None:
        print(f"[Trigger] {self.TRIGGER_EVENT} on svc-{self.TRIGGER_ROLE} for {self.trigger_seconds:.0f}s")
        self._pulse(True)
        try:
            time.sleep(self.trigger_seconds)
        finally:
            self._pulse(False)
        print("[Trigger] Event cleared")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._pulse(False)
        self.set_caller_mesh(DEFAULT_MESH)
        self.set_backend_pool(self.backend_db["pool_size"], self.backend_db["max_overflow"])
        print("Restored the default mesh policy on svc-message and svc-channel's pool")


class LoadSpikeRPCRetryStormSlack(ChannelRetryStormBase):
    """Trigger: a one-shot load spike of extra senders (the original: 30 s at a multiple of the base rate)."""

    backend_db = {"pool_size": 6, "max_overflow": 2}
    SPIKE_RPS = 60
    SPIKE_SECONDS = 30

    def __init__(self):
        mesh = self.caller_mesh
        super().__init__(
            component="svc-message -> svc-channel mesh retry policy (roles.message.mesh)",
            description=(
                "A metastable retry storm on the send path triggered by a load spike. Every message send "
                "(svc-message POST /messages) resolves channel authz on svc-channel (GET /authz/resolve) through "
                "svc-message's mesh client, whose policy (`roles.message.mesh` in ConfigMap app-config, also returned "
                f"by svc-message GET /admin/config) is aggressive: {mesh['retries']} attempts, retryOnTimeout=true, "
                f"{mesh['perTryTimeoutMs']} ms per-try timeout, no backoff. svc-channel reads ACLs in strict mode "
                f"(ACTIVE_EVENTS=read_consistency_strict, ACL_HOLD_MS={self.backend_env['ACL_HOLD_MS']}) through a "
                f"{self.backend_db['pool_size']}+{self.backend_db['max_overflow']}-connection pool (`roles.channel.db`), "
                "enough for base traffic. A transient burst of sends (an extra ~"
                f"{self.SPIKE_RPS}/s for {self.SPIKE_SECONDS} s, already over) queued resolves past the 300 ms per-try "
                "timeout; svc-message retried the timed-out calls while the abandoned attempts still waited for and "
                "held channel connections, so ~3 attempts per send keep demand above the pool's capacity at base "
                "load: the pool stays saturated, every attempt times out and sends fail with 503 "
                "`authz_unavailable`. The sustaining cause is the timeout/retry/queue feedback loop, not the spike. "
                "Valid mitigations bring the retry policy (fewer attempts, a per-try timeout above the queueing delay, "
                "no retry on timeout, or a sane breaker) and/or svc-channel's pool (within the peer-uniform 20+10) "
                "into a safe envelope so the backlog drains and a repeated spike is survived; disabling strict ACL "
                "reads or the authz check is not a fix."
            ),
        )

    def tip(self) -> None:
        print(f"[Trigger] load spike: +{self.SPIKE_RPS} sends/s for {self.SPIKE_SECONDS}s")
        script = _SPIKE_SCRIPT.format(rps=self.SPIKE_RPS, seconds=self.SPIKE_SECONDS)
        print(self.app.toolbox_exec("python3 -c " + shlex.quote(script), timeout=self.SPIKE_SECONDS + 60).strip())


# Original registry id -> (ported problem id, problem class). Variants of one
# fault on different original apps map to the same port.
PORTS: dict[str, tuple[str, type]] = {
    **{
        f"stale_coredns_config_{app}": ("stale_coredns_config_slack_spine", StaleCoreDNSConfigSlack)
        for app in ("astronomy_shop", "social_network")
    },
    "admission_webhook_tls_mismatch_hotel_reservation": (
        "admission_webhook_tls_mismatch_slack_spine",
        AdmissionWebhookTLSMismatchSlack,
    ),
    "cumulative_admission_webhook_timeout_hotel_reservation": (
        "cumulative_admission_webhook_timeout_slack_spine",
        CumulativeAdmissionWebhookTimeoutSlack,
    ),
    "pod_cidr_exhaustion_hotel_reservation": ("pod_cidr_exhaustion_slack_spine", PodCIDRExhaustionSlack),
    "feature_flag_latent_bug_hotel_reservation": ("feature_flag_latent_bug_slack_spine", FeatureFlagLatentBugSlack),
    "astronomy_shop_ad_service_image_slow_load": ("image_slow_load_slack_spine", ImageSlowLoadSlack),
    "calico_route_reflector_label_drift_hotel_reservation": (
        "calico_route_reflector_label_drift_slack_spine",
        CalicoRouteReflectorLabelDriftSlack,
    ),
    "stale_hostaliases_dns_poisoning_astronomy_shop": (
        "stale_hostaliases_dns_poisoning_slack_spine",
        StaleHostAliasesSlack,
    ),
    "ingress_misroute": ("ingress_misroute_slack_spine", IngressMisrouteSlack),
    "capacity_decrease_rpc_retry_storm": (
        "capacity_decrease_rpc_retry_storm_slack_spine",
        CapacityDecreaseRPCRetryStormSlack,
    ),
    "load_spike_rpc_retry_storm": ("load_spike_rpc_retry_storm_slack_spine", LoadSpikeRPCRetryStormSlack),
}
