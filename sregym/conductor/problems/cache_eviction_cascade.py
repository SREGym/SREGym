import time

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.cache_eviction_cascade_mitigation import (
    AvailabilityDamageOracle,
    ValkeyConfigOracle,
)
from sregym.conductor.oracles.compound import CompoundedOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.sustained_readiness import SustainedReadinessOracle
from sregym.conductor.oracles.workload import WorkloadOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_app import ApplicationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class CacheEvictionCascade(Problem):
    """Cache eviction cascade under sustained high traffic.

    Scenario: A sudden traffic spike (e.g., holiday shopping rush) hits the cluster
    while valkey-cart's maxmemory is misconfigured to 1Mi. The tiny cache evicts entries
    aggressively, causing cache misses that cascade through backend services.

    The elevated load represents real user traffic — the agent should NOT reduce it.
    The root cause is purely the cache misconfiguration in the valkey-cart deployment.

    This problem is designed to produce graduated mitigation scores: different approaches
    to stopping the alerts have different availability costs.
    """

    INJECTED_MAXMEMORY = 1048576  # 1Mi
    INJECTED_POLICY = "allkeys-lru"
    INJECTED_USERS = 200
    INJECTED_SPAWN_RATE = 50
    ORIGINAL_USERS = 10
    ORIGINAL_SPAWN_RATE = 1
    _IMPACT_SAMPLE_ENTRIES = 20

    def __init__(self):
        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.faulty_service = "valkey-cart"
        self.kubectl = KubeCtl()

        # Fault impact baseline — populated during inject_fault()
        self.fault_impact_snapshot = None

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The valkey-cart deployment command was modified to enforce a maxmemory limit "
                "of 1Mi with allkeys-lru eviction policy. Under sustained high user traffic, "
                "the tiny cache evicts entries aggressively, causing cache misses that cascade "
                "to backend services (cart, checkout, frontend) and degrade the entire request path."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        self.mitigation_oracle = CompoundedOracle(
            self,
            ValkeyConfigOracle(problem=self),
            AlertOracle(problem=self),
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),
            SustainedReadinessOracle(problem=self),
            AvailabilityDamageOracle(problem=self),
        )

    @mark_fault_injected
    def inject_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)

        # Step 1: Patch valkey-cart deployment with restrictive maxmemory
        print("[1/4] Patching valkey-cart deployment with maxmemory=1Mi...")
        injector.inject_valkey_maxmemory_reduction(
            maxmemory=self.INJECTED_MAXMEMORY,
            policy=self.INJECTED_POLICY,
        )

        # Step 2: Simulate traffic spike (real user traffic — agent should not reduce this)
        print("[2/4] Increasing load to simulate traffic spike...")
        self.app.wrk.change_users(self.INJECTED_USERS, self.namespace)
        self.app.wrk.change_spawn_rate(self.INJECTED_SPAWN_RATE, self.namespace)

        # Step 3: Wait for cascade to establish
        print("[3/4] Waiting 30s for cascade to establish under high load...")
        time.sleep(30)

        # Step 4: Capture fault impact snapshot — proves user requests are impacted
        print("[4/4] Capturing fault impact snapshot...")
        self.fault_impact_snapshot = self._capture_impact_snapshot()
        print("[FAULT INJECTED] Cache eviction cascade established.")

    def _capture_impact_snapshot(self) -> dict:
        """Sample workload during fault to record baseline impact on user requests.

        This snapshot serves two purposes:
        1. Evidence that user requests are actually impacted by the fault
        2. Baseline for computing recovery percentage after mitigation
        """
        snapshot = {
            "error_rate": 0.0,
            "total_entries": 0,
            "total_errors": 0,
            "total_requests": 0,
            "timestamp": time.time(),
        }

        try:
            self.app.wrk.collect(number=1)  # Prime
            entries = self.app.wrk.collect(number=self._IMPACT_SAMPLE_ENTRIES)

            if not entries:
                print("[⚠️] No workload entries during fault — cannot capture impact")
                return snapshot

            error_count = sum(1 for e in entries if not e.ok)
            total_requests = sum(e.number for e in entries)
            total = len(entries)
            error_rate = error_count / total if total > 0 else 0.0

            snapshot["error_rate"] = error_rate
            snapshot["total_entries"] = total
            snapshot["total_errors"] = error_count
            snapshot["total_requests"] = total_requests

            print(f"[📊] Fault impact snapshot:")
            print(f"     Entries: {total}, Errors: {error_count}, Error rate: {error_rate:.2%}")
            print(f"     Total requests: {total_requests}")

            if error_rate > 0.1:
                print(f"[✅] Confirmed: user requests are impacted (error_rate={error_rate:.2%})")
            else:
                print(f"[⚠️] Low error rate during fault ({error_rate:.2%}) — cascade may not be fully established")

        except Exception as e:
            print(f"[⚠️] Error capturing fault impact: {e}")

        return snapshot

    @mark_fault_injected
    def recover_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)

        # Restore valkey-cart deployment
        print("[1/3] Restoring valkey-cart deployment...")
        injector.recover_valkey_maxmemory_reduction()

        # Restore original load levels
        print("[2/3] Restoring original traffic levels...")
        self.app.wrk.change_users(self.ORIGINAL_USERS, self.namespace)
        self.app.wrk.change_spawn_rate(self.ORIGINAL_SPAWN_RATE, self.namespace)

        # Rolling restart cart to clear stale connections
        print("[3/3] Rolling restart cart deployment...")
        self.kubectl.exec_command(
            f"kubectl rollout restart deployment/cart -n {self.namespace}"
        )
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/cart -n {self.namespace} --timeout=120s"
        )
        print("[FAULT RECOVERED] Cache eviction cascade resolved.")
