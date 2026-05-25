"""
Cascading Failure via Workload Resource Exhaustion
===================================================

Real-World Story
----------------
In production MLOps and data-intensive environments, teams frequently deploy
workloads—such as AI inference pipelines or batch processors—without precise
resource `requests` and `limits`. The workload gradually consumes all available
Node memory, forcing the Linux kernel OOM-Killer to terminate the most
memory-hungry Pods. Because Kubernetes scheduling is trust-based, other
critical services (databases, API gateways) in the same namespace share the
same Node pool and become collateral victims of the runaway workload.

A well-documented real-world example occurred in a payment-processing company
where an ML feature-engineering job deployed without memory limits caused
PostgreSQL and the API gateway to be OOM-killed simultaneously, producing a
full-system outage lasting 47 minutes.
Reference: https://k8s.af / https://github.com/hjacobs/kubernetes-failure-stories

Simulation on SREGym
--------------------
We deploy the Hotel Reservation application and inject two simultaneous faults:

1. **Runaway Pod (Memory Hog):** A dedicated `stress-ng` Pod is deployed into
   the application namespace with *no* resource limits. It continuously
   allocates memory, simulating an unguarded ML batch workload.

2. **Tight Memory Limit on `mongodb-rate`:** The `mongodb-rate` deployment
   receives an artificially low memory limit (32 Mi), causing OOMKilled
   restarts once the Node comes under memory pressure.

Together, these produce the cascading failure signature: the stress pod hogs
memory → the kernel OOM-kills `mongodb-rate` containers → the rate service
becomes unavailable → hotel reservation requests fail.

Runtime Behaviour
-----------------
- `kubectl get pods -n hotel-reservation` shows `mongodb-rate-*` pods in
  `OOMKilled` / `CrashLoopBackOff` state.
- `kubectl describe pod <mongodb-rate-pod> -n hotel-reservation` shows
  `Last State: OOMKilled` and `Exit Code: 137`.
- `kubectl top nodes` (if metrics-server is installed) shows near-100% memory
  usage on the node.
- The stress pod itself appears `Running` with no resource constraints visible
  in `kubectl describe pod stress-hog -n hotel-reservation`.

Agent Behaviour (Stratus / AI Agent)
--------------------------------------
A well-functioning agent should:
1. Detect `CrashLoopBackOff` / OOMKilled events on `mongodb-rate`.
2. Correlate the stress pod's unbounded memory consumption as the root cause.
3. Delete or resource-cap the stress pod.
4. Patch `mongodb-rate` to raise (or remove) the tight memory limit.
5. Confirm all pods return to `Running`.

The mitigation oracle verifies:
- The `stress-hog` Pod is gone (or has resource limits applied).
- `mongodb-rate` deployment has `resources.limits.memory` ≥ 128 Mi.
- All application pods are `Running` and `Ready`.
"""

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.conductor.oracles.cascading_oom_mitigation import CascadingOOMMitigationOracle
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------
_STRESS_POD_NAME = "stress-hog"

# Memory the stress pod will try to allocate (keeps growing to simulate a leak).
# Expressed as a string passed directly to stress-ng --vm-bytes.
# Use a value large enough to pressure the node but not instantly crash Kind.
_STRESS_VM_BYTES = "256M"

# Tight limit imposed on mongodb-rate to make it the first victim of OOM pressure.
_VICTIM_MEMORY_LIMIT = "32Mi"

# Name of the victim deployment inside the hotel-reservation namespace.
_VICTIM_DEPLOYMENT = "mongodb-rate"


class CascadingOOMHotelReservation(Problem):
    """
    Injects a cascading OOM failure into the Hotel Reservation application.

    Fault 1 — Runaway Pod:
        Deploys a `stress-ng` Pod with no resource limits into the application
        namespace.  The pod continuously allocates `_STRESS_VM_BYTES` of
        memory, simulating an unconstrained ML/batch workload.

    Fault 2 — Tight Victim Limit:
        Patches `mongodb-rate` with a memory limit of `_VICTIM_MEMORY_LIMIT`
        so it is the first service to be OOM-killed as node pressure rises.
    """

    def __init__(self):
        self.app = HotelReservation()
        super().__init__(app=self.app, namespace=self.app.namespace)

        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace

        self.faulty_service = _VICTIM_DEPLOYMENT
        self.stress_pod_name = _STRESS_POD_NAME

        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"An unconstrained workload Pod (`{_STRESS_POD_NAME}`) was deployed into namespace "
                f"`{self.namespace}` without any resource `limits`, causing it to continuously "
                f"allocate memory (~{_STRESS_VM_BYTES}). This exhausts available Node memory and "
                f"forces the Linux OOM-Killer to terminate containers in the `{_VICTIM_DEPLOYMENT}` "
                f"deployment, which was already constrained to a low memory limit "
                f"(`{_VICTIM_MEMORY_LIMIT}`). The resulting cascading OOMKilled restarts make the "
                "rate-service unavailable and degrade overall Hotel Reservation functionality. "
                "Root cause: missing resource limits on the stress workload combined with an "
                "under-provisioned memory limit on the victim service."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = CascadingOOMMitigationOracle(problem=self)

        # Deploy application workload
        self.app.create_workload()

    # ------------------------------------------------------------------
    # Fault Injection
    # ------------------------------------------------------------------

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection: Cascading OOM ==")

        # --- Fault 1: Deploy the runaway memory-hog Pod ---
        self._deploy_stress_pod()

        # --- Fault 2: Clamp mongodb-rate to a tiny memory limit ---
        self._tighten_victim_memory_limit()

        print(
            f"[INJECTED] stress pod='{self.stress_pod_name}' | "
            f"victim='{self.faulty_service}' limit={_VICTIM_MEMORY_LIMIT} | "
            f"namespace={self.namespace}"
        )

    def _deploy_stress_pod(self):
        """Deploy a Pod that continuously allocates memory with no resource limits."""
        pod_manifest = f"""
apiVersion: v1
kind: Pod

metadata:
  name: {self.stress_pod_name}
  namespace: {self.namespace}
  labels:
    app: load-generator
    role: stress-test
spec:
  restartPolicy: Always
  containers:
  - name: stress
    image: alexeiled/stress-ng:latest-ubuntu
    command: ["stress-ng"]
    args:
      - "--vm"
      - "1"
      - "--vm-bytes"
      - "{_STRESS_VM_BYTES}"
      - "--vm-keep"
      - "--timeout"
      - "0"
    # Intentionally NO resources.limits — this is the fault.
"""
        # Write manifest to a temp file and apply
        manifest_path = f"/tmp/{self.stress_pod_name}.yaml"
        with open(manifest_path, "w") as f:
            f.write(pod_manifest)

        result = self.kubectl.exec_command(
            f"kubectl apply -f {manifest_path}"
        )
        print(f"[STRESS POD] Deployed: {result}")

    def _tighten_victim_memory_limit(self):
        container_name = "hotel-reserv-rate-mongo"
        patch = (
            '{"spec":{"template":{"spec":{"containers":[{'
            f'"name":"{container_name}",'
            f'"resources":{{"limits":{{"memory":"{_VICTIM_MEMORY_LIMIT}"}}}}'
            "}]}}}}"
        )
        result = self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} "
            f"-n {self.namespace} --type=strategic -p '{patch}'"
        )
        print(f"[VICTIM PATCH] mongodb-rate memory limit → {_VICTIM_MEMORY_LIMIT}: {result}")
    # ------------------------------------------------------------------
    # Fault Recovery
    # ------------------------------------------------------------------

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery: Cascading OOM ==")

        # --- Remove the stress pod ---
        result = self.kubectl.exec_command(
            f"kubectl delete pod {self.stress_pod_name} "
            f"-n {self.namespace} --ignore-not-found=true"
        )
        print(f"[RECOVERY] Deleted stress pod: {result}")

        # --- Remove the tight memory limit from mongodb-rate ---
        # JSON-patch to remove the limits.memory key entirely
        patch = '[{"op":"remove","path":"/spec/template/spec/containers/0/resources/limits/memory"}]'
        result = self.kubectl.exec_command(
            f"kubectl patch deployment {self.faulty_service} "
            f"-n {self.namespace} --type=json -p '{patch}'"
        )
        print(f"[RECOVERY] Removed memory limit from {self.faulty_service}: {result}")

	

