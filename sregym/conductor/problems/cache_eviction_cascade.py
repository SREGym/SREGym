import time

from sregym.conductor.oracles.alert_oracle import AlertOracle
from sregym.conductor.oracles.cache_eviction_cascade_mitigation import (
    AvailabilityDamageOracle,
    CartResourceOracle,
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
    """Cache eviction cascade under sustained high traffic — compound fault.

    Scenario: A sudden traffic spike (holiday shopping rush) hits the cluster while
    two faults are present:

      1. valkey-cart is loaded from a ConfigMap (valkey-cart-config) that sets
         maxmemory to 1Mi with allkeys-lru eviction. The deployment command is
         `valkey-server /usr/local/etc/valkey/valkey.conf` — which looks normal.
         Agents must inspect the ConfigMap to discover the restriction.

      2. The cart deployment has its CPU limit reduced to 50m, starving it of CPU
         under the high traffic load. Even if valkey is fixed, cart remains throttled
         until this is also addressed.

    Full mitigation requires:
      - Finding and fixing the ConfigMap (or deleting it)
      - Finding and removing/raising the cart CPU limit
      - Restarting cart to clear stale connections
      - Verifying the system has actually recovered under load

    The elevated load (200 Locust users) represents real customer traffic and must
    NOT be reduced by the agent.
    """

    INJECTED_MAXMEMORY = 1048576  # 1Mi
    INJECTED_POLICY = "allkeys-lru"
    INJECTED_CART_CPU = "50m"
    INJECTED_USERS = 200
    INJECTED_SPAWN_RATE = 50
    ORIGINAL_USERS = 10
    ORIGINAL_SPAWN_RATE = 1

    def __init__(self):
        self.app = AstronomyShop()
        self.namespace = self.app.namespace
        super().__init__(app=self.app, namespace=self.namespace)

        self.faulty_service = "valkey-cart"
        self.kubectl = KubeCtl()
        self.fault_impact_snapshot = None

        self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}, deployment/cart",
            namespace=self.namespace,
            description=(
                "Two faults combine under sustained high traffic (200 users): "
                "(1) ConfigMap valkey-cart-config sets maxmemory to 1Mi with allkeys-lru eviction policy, "
                "mounted into the valkey-cart deployment as /usr/local/etc/valkey/valkey.conf — "
                "the tiny cache evicts entries aggressively, causing cache misses that cascade to "
                "cart, checkout, and frontend. "
                "(2) The cart deployment has its CPU limit reduced to 50m, causing severe CPU throttling "
                "under high load — even after valkey is fixed, cart remains unable to serve requests "
                "at the required rate until this constraint is also removed."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()

        self.mitigation_oracle = CompoundedOracle(
            self,
            ValkeyConfigOracle(problem=self),           # importance=2.0
            CartResourceOracle(problem=self),            # importance=2.0
            AlertOracle(problem=self),                   # importance=1.0
            WorkloadOracle(problem=self, wrk_manager=self.app.wrk),  # importance=3.0
            SustainedReadinessOracle(problem=self),      # importance=1.0
            AvailabilityDamageOracle(problem=self),      # importance=2.5
        )

    @mark_fault_injected
    def inject_fault(self):
        injector = ApplicationFaultInjector(namespace=self.namespace)

        print("[1/5] Injecting valkey maxmemory restriction via ConfigMap...")
        injector.inject_valkey_maxmemory_reduction(
            maxmemory=self.INJECTED_MAXMEMORY,
            policy=self.INJECTED_POLICY,
        )

        print("[2/5] Injecting cart CPU throttle (50m)...")
        injector.inject_cart_cpu_throttle(cpu_limit=self.INJECTED_CART_CPU)

        print("[3/5] Simulating traffic spike (200 users)...")
        self.app.wrk.change_users(self.INJECTED_USERS, self.namespace)
        self.app.wrk.change_spawn_rate(self.INJECTED_SPAWN_RATE, self.namespace)

        print("[4/5] Waiting 30s for cascade to establish under high load...")
        time.sleep(30)

        print("[5/5] Capturing fault impact snapshot (60s window)...")
        self.fault_impact_snapshot = self._capture_impact_snapshot()
        print("[FAULT INJECTED] Compound cache+CPU cascade established.")

    def _capture_impact_snapshot(self) -> dict:
        """Sample workload during fault to establish baseline impact and prove user requests are affected."""
        snapshot = {
            "error_rate": 0.0,
            "total_entries": 0,
            "total_errors": 0,
            "total_requests": 0,
            "timestamp": time.time(),
        }

        print("[⏳] Waiting 60s for workload data to accumulate under fault...")
        time.sleep(60)

        try:
            self.app.wrk._extractlog()
            entries = self.app.wrk.recent_entries(duration=60)

            if not entries:
                print("[⚠️] No workload entries during fault — cascade may not have established")
                return snapshot

            error_count = sum(1 for e in entries if not e.ok)
            total_requests = sum(e.number for e in entries)
            total = len(entries)
            error_rate = error_count / total if total > 0 else 0.0

            snapshot.update({
                "error_rate": error_rate,
                "total_entries": total,
                "total_errors": error_count,
                "total_requests": total_requests,
            })

            print(f"[📊] Fault impact snapshot: {total} entries, {error_count} errors, "
                  f"error_rate={error_rate:.2%}, total_requests={total_requests}")

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

        print("[1/4] Restoring valkey-cart deployment and removing ConfigMap...")
        injector.recover_valkey_maxmemory_reduction()

        print("[2/4] Removing cart CPU throttle...")
        injector.recover_cart_cpu_throttle()

        print("[3/4] Restoring original traffic levels...")
        self.app.wrk.change_users(self.ORIGINAL_USERS, self.namespace)
        self.app.wrk.change_spawn_rate(self.ORIGINAL_SPAWN_RATE, self.namespace)

        print("[4/4] Rolling restart cart to clear stale connections...")
        self.kubectl.exec_command(
            f"kubectl rollout restart deployment/cart -n {self.namespace}"
        )
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/cart -n {self.namespace} --timeout=120s"
        )
        print("[FAULT RECOVERED] Compound cache+CPU cascade resolved.")
