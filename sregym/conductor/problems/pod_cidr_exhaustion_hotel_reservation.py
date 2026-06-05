import os
import tempfile
import time

from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class PodCIDRExhaustionHotelReservation(Problem):
    """
    Simulates a real-world GKE outage where the pod IP secondary range
    was exhausted due to per-node pre-allocation.

    Real-world reference: https://deploy.live/blog/when-gke-ran-out-of-ip-addresses/

    Simulation approach (works on /16 clusters, no cluster CIDR changes needed):
    - Disable the default IPPool and create a tiny exhaustible pool
    - Delete existing node block affinities so nodes must allocate from the tiny pool
    - Deploy batch-worker to exhaust the tiny pool
    - Force-delete HR pods so they cannot reschedule
    - Agent mitigation: scale down batch-worker in batch-jobs namespace

    Requires Calico CNI (works with both /16 and /24 cluster CIDRs).
    """

    EXHAUST_NAMESPACE = "data-processing"
    EXHAUST_DEPLOYMENT = "data-pipeline"
    TINY_POOL_NAME = "workload-pool"
    TINY_POOL_CIDR = "192.168.254.0/26"  # 64 IPs — enough for batch-worker, too few for HR
    DEFAULT_POOL_NAME = "default-ipv4-ippool"
    NUM_EXHAUST_PODS = 60  # enough to exhaust a /26 with strictAffinity

    def __init__(self, faulty_service: str = "frontend"):
        self.faulty_service = faulty_service
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.kubectl = KubeCtl()
        super().__init__(app=self.app, namespace=self.namespace)

        self.root_cause = self.build_structured_root_cause(
            component="cluster-networking",
            namespace=self.namespace,
            description=(
                "Calico IPAM IP exhaustion: the cluster's available IP address pool has been "
                "exhausted and strictAffinity is enabled, preventing nodes from borrowing IP "
                "blocks from each other. Hotel Reservation pods cannot obtain IP addresses "
                "and are stuck in ContainerCreating state. Evidence: 'failed to request IPv4 "
                "addresses: Assigned 0 out of 1 requested IPv4 addresses; No more free "
                "affine blocks and strict affinity enabled'. Mitigation requires either "
                "freeing IP allocations by scaling down consuming workloads or restoring "
                "the default IP pool."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    def _apply_manifest(self, manifest: str) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(manifest)
            tmp_path = f.name
        self.kubectl.exec_command(f"kubectl apply -f {tmp_path}")
        os.unlink(tmp_path)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        # Step 1: Enable strictAffinity
        print("Enabling Calico strictAffinity...")
        self.kubectl.exec_command(
            'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":true}}\''
        )

        # Step 2: Create tiny pool
        print(f"Creating tiny IPPool '{self.TINY_POOL_NAME}' ({self.TINY_POOL_CIDR})...")
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

        # Step 3: Disable default pool
        print(f"Disabling default IPPool '{self.DEFAULT_POOL_NAME}'...")
        self.kubectl.exec_command(
            f'kubectl patch ippool {self.DEFAULT_POOL_NAME} --type=merge -p \'{{"spec":{{"disabled":true}}}}\''
        )

        # Step 4: Create batch-jobs namespace
        self.kubectl.exec_command(
            f"kubectl create namespace {self.EXHAUST_NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -"
        )

        # Step 5: Deploy batch-worker to exhaust the tiny pool
        print(f"Deploying batch-worker with {self.NUM_EXHAUST_PODS} replicas...")
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

        print("Waiting for batch-worker pods to be scheduled and consume IPs...")
        for _ in range(30):
            result = self.kubectl.exec_command(f"kubectl get pods -n {self.EXHAUST_NAMESPACE} --no-headers")
            if result:
                scheduled = sum(
                    1 for line in result.strip().split("\n") if "Running" in line or "ContainerCreating" in line
                )
                if scheduled >= self.NUM_EXHAUST_PODS - 5:
                    break
            time.sleep(2)

        # Step 6: Force delete HR pods
        print("Force deleting Hotel Reservation pods to trigger rescheduling...")
        self.kubectl.exec_command(f"kubectl delete pods --all -n {self.namespace} --force --grace-period=0")
        print("IP pool exhausted. Hotel Reservation pods cannot reschedule.")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # Scale down batch-worker
        self.kubectl.exec_command(
            f"kubectl scale deployment {self.EXHAUST_DEPLOYMENT} -n {self.EXHAUST_NAMESPACE} --replicas=0"
        )
        print("Scaled down batch-worker deployment to 0 replicas")

        # Re-enable default pool
        print(f"Re-enabling default IPPool '{self.DEFAULT_POOL_NAME}'...")
        self.kubectl.exec_command(
            f'kubectl patch ippool {self.DEFAULT_POOL_NAME} --type=merge -p \'{{"spec":{{"disabled":false}}}}\''
        )

        # Delete tiny pool
        print(f"Deleting tiny IPPool '{self.TINY_POOL_NAME}'...")
        self.kubectl.exec_command(f"kubectl delete ippool {self.TINY_POOL_NAME} --ignore-not-found")

        # Disable strictAffinity
        print("Disabling Calico strictAffinity...")
        self.kubectl.exec_command(
            'kubectl patch ipamconfig default --type=merge -p \'{"spec":{"strictAffinity":false}}\''
        )

        # Delete namespace
        self.kubectl.exec_command(f"kubectl delete namespace {self.EXHAUST_NAMESPACE} --ignore-not-found")
        print(f"Deleted namespace: {self.EXHAUST_NAMESPACE}")

        # Wait and restart HR
        print("Waiting for Calico to reclaim IP allocations...")
        time.sleep(30)
        self.kubectl.exec_command(f"kubectl rollout restart deployment -n {self.namespace}")
        self.kubectl.wait_for_stable(self.namespace)
        print("Recovery complete")
