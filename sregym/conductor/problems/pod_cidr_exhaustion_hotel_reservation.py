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

    Real-world reference: https://blog.deleu.dev/when-gke-ran-out-of-ip-addresses/

    In the real incident, GKE pre-allocates 256 IPs per node (2x max-pods-per-node).
    With a /16 subnet and 256 nodes, all 65,536 IPs were consumed, preventing
    the cluster autoscaler from adding new nodes and leaving new pods Pending.

    Here we simulate the same symptom on a Kind+Calico cluster with a small
    /24 IP pool: a batch-jobs deployment consumes all available IPs, then
    Hotel Reservation pods are force-deleted so they cannot reschedule.

    Requires Calico CNI with strictAffinity: true in IPAMConfig.
    """

    EXHAUST_NAMESPACE = "batch-jobs"
    EXHAUST_DEPLOYMENT = "batch-worker"
    NUM_EXHAUST_PODS = 190

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
                "The cluster's pod IP pool (10.244.0.0/24) has been exhausted by a "
                "batch-jobs deployment consuming all available Calico IP allocations "
                "across all worker nodes. Hotel Reservation pods cannot obtain IP "
                "addresses and are stuck in ContainerCreating state with "
                "'failed to request IPv4 addresses: Assigned 0 out of 1 requested "
                "IPv4 addresses; No more free affine blocks and strict affinity "
                "enabled' errors. This simulates the real-world GKE incident where "
                "per-node IP pre-allocation exhausted the VPC secondary range, "
                "preventing new pods from scheduling. Users observe complete service "
                "unavailability. Mitigation requires scaling down the batch-worker "
                "deployment in the batch-jobs namespace sufficiently to free IP "
                "allocations for Hotel Reservation pods to recover."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.app.create_workload()
        self.mitigation_oracle = MitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")

        # Step 1: Create batch-jobs namespace
        self.kubectl.exec_command(
            f"kubectl create namespace {self.EXHAUST_NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -"
        )

        # Step 2: Create a batch-worker deployment that floods the cluster
        # with pods to consume all available Calico IP allocations.
        # No nodeName constraint — let Calico distribute freely until exhausted.
        deployment_manifest = f"""apiVersion: apps/v1
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
    spec:
      containers:
      - name: worker
        image: registry.k8s.io/pause:3.9
        resources:
          requests:
            cpu: "1m"
            memory: "1Mi"
"""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(deployment_manifest)
            tmp_path = f.name
        self.kubectl.exec_command(f"kubectl apply -f {tmp_path}")
        os.unlink(tmp_path)

        print(f"Created batch-worker deployment with {self.NUM_EXHAUST_PODS} replicas")

        # Step 3: Wait for Calico to assign IPs and exhaust the pool
        print("Waiting for Calico to assign IPs and exhaust the pool...")
        time.sleep(15)

        # Step 4: Force delete HR pods so they must reschedule
        # onto the now-exhausted cluster
        print("Force deleting Hotel Reservation pods to trigger rescheduling...")
        self.kubectl.exec_command(f"kubectl delete pods --all -n {self.namespace} --force --grace-period=0")
        print(f"IP pool exhausted. Service: {self.faulty_service} | Namespace: {self.namespace}")

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")

        # Scale down the batch-worker deployment to free IP allocations.
        # This mirrors the real-world fix of reducing --max-pods-per-node
        # to free up IP consumption.
        self.kubectl.exec_command(
            f"kubectl scale deployment {self.EXHAUST_DEPLOYMENT} -n {self.EXHAUST_NAMESPACE} --replicas=0"
        )
        print("Scaled down batch-worker deployment to 0 replicas")

        # Wait for Calico to reclaim IPs
        print("Waiting for Calico to reclaim IP allocations...")
        time.sleep(60)

        # Delete the namespace
        self.kubectl.exec_command(f"kubectl delete namespace {self.EXHAUST_NAMESPACE} --ignore-not-found")
        print(f"Deleted namespace: {self.EXHAUST_NAMESPACE}")

        # Restart all deployments and wait for stability
        self.kubectl.exec_command(f"kubectl rollout restart deployment -n {self.namespace}")
        self.kubectl.wait_for_stable(self.namespace)
        print("Recovery complete")
