import math
import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle


class MissingStaleCacheFallbackMitigationOracle(MitigationOracle):
    """
    Mitigation oracle for the missing stale-cache fallback problem.

    The incident is a product-catalog slowdown that recommendation cannot survive. Since the slowdown
    is part of the incident, not something the agent is expected to remove, the oracle keeps it in
    place while evaluating the fix, and checks that recommendation now degrades gracefully.

    A mitigation only passes if all of the following are true:

      1. The application stays healthy. No deployment deleted, scaled to 0, or left with unready pods
         (inherited from MitigationOracle).
      2. Recommendation works normally when product-catalog is healthy. The oracle temporarily removes
         the delay and warms the recommendation cache first, since the agent's fix restarts the service
         and clears its in-memory cache.
      3. After the delay is applied again to the current pods, recommendation continues to work despite
         the slowdown. At least 95% of requests must succeed and p95 latency stays below 2 seconds.
         Simply increasing the timeout fails because requests would still wait for the full delay.
      4. The responses are actually valid: each contains 1-5 real product IDs, never includes the product
         being viewed, and isn't the same hard-coded list every time. This rejects empty, stubbed, or
         fixed responses.
      5. Once product-catalog is healthy again, new catalog data shows up within 60 seconds. The oracle
         inserts a new product and verifies that it eventually appears in recommendation results. This rejects
         caching forever and makes "bounded staleness" concrete.

    The oracle restores the environment before it exits. If the keep-alive delay was running when grading started,
    it is restored before grading finishes.
    """

    PROBE_COUNT = 20
    PROBE_INTERVAL_S = 0.5
    PROBE_DEADLINE_S = 5.0
    MIN_SUCCESS_RATE = 0.95
    # A valid fix that keeps the 1 second timeout answers in 1.0-1.5 s under the delay
    P95_SLO_S = 2.0
    MAX_RECOMMENDATIONS = 5
    FRESHNESS_WINDOW_S = 60
    FRESHNESS_POLL_S = 2.0

    # Rollout settle (60 s) + lifting/applying the delay + warm-up + 20 probes + the freshness window.
    evaluation_timeout_seconds = 600

    FAILURE_CLASSES = {
        "recommendations_failing_when_healthy": FailureClass.AGENT_ERROR,
        "recommendations_failing_under_degradation": FailureClass.AGENT_ERROR,
        "recommendations_too_slow_under_degradation": FailureClass.AGENT_ERROR,
        "recommendations_empty": FailureClass.AGENT_ERROR,
        "recommendations_invalid": FailureClass.AGENT_ERROR,
        "recommendations_constant": FailureClass.AGENT_ERROR,
        "catalog_changes_not_picked_up": FailureClass.AGENT_ERROR,
        "impairment_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "probe_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "freshness_probe_setup_failed": FailureClass.ENVIRONMENT_ERROR,
    }

    def evaluate(self) -> dict:
        print("=== Mitigation Evaluation (recommendation stale-cache fallback) ===")

        base = super().evaluate()
        if not base.get("success"):
            return base

        problem = self.problem
        incident_was_running = self._incident_running()
        try:
            return self._evaluate_behaviour()
        finally:
            try:
                if incident_was_running:
                    problem.apply_delay(keep_alive=True)
                else:
                    problem.remove_delay()
            except Exception as exc:
                print(f"WARNING: Could not restore the upstream impairment state: {exc}")

    def _evaluate_behaviour(self) -> dict:
        problem = self.problem

        # 2. Healthy window: lift any impairment, wait for the service, warm its cache.
        try:
            problem.remove_delay()
        except RuntimeError as exc:
            print(f"ERROR: could not lift the product-catalog delay: {exc}")
            return self.fail("impairment_unavailable", message=f"could not lift the delay: {exc}")

        try:
            problem.wait_until_answering()
        except RuntimeError as exc:
            print(f"ERROR: recommendation does not answer even with product-catalog healthy: {exc}")
            return self.fail("recommendations_failing_when_healthy", message=str(exc))

        warmup = problem.probe_recommendations(problem.WARMUP_CALLS, 0.5, self.PROBE_DEADLINE_S)

        if not all(r["ok"] for r in warmup):
            print(f"ERROR: recommendation fails with product-catalog healthy: {self._summary(warmup)}")
            return self.fail("recommendations_failing_when_healthy", message=self._summary(warmup))

        print(f"SUCCESS: healthy path answers ({self._summary(warmup)})")

        # 3. Steady impairment on the current pods, verified before measuring.
        try:
            problem.apply_delay(keep_alive=False)
        except RuntimeError as exc:
            print(f"ERROR: could not re-apply the product-catalog delay: {exc}")
            return self.fail("impairment_unavailable", message=str(exc))

        if not problem.wait_for_delay(active=True):
            print("ERROR: could not re-apply the product-catalog delay")
            return self.fail("impairment_unavailable", message=f"{problem.delay} delay never became active")

        print(f"SUCCESS: product-catalog delay ({problem.delay}) active on the current pods")

        results = problem.probe_recommendations(self.PROBE_COUNT, self.PROBE_INTERVAL_S, self.PROBE_DEADLINE_S)
        ok = [r for r in results if r["ok"]]
        success_rate = len(ok) / len(results)
        p95_s = self._p95([r["ms"] for r in results]) / 1000
        summary = self._summary(results)

        if any(r.get("code") in ("EXEC_FAILED", "NO_OUTPUT") for r in results):
            # The probe itself could not run (kubectl exec failed).
            print(f"ERROR: could not probe recommendation: {summary}")
            return self.fail("probe_unavailable", message=summary)

        if success_rate < self.MIN_SUCCESS_RATE:
            print(f"ERROR: recommendation fails under the upstream delay: {summary}")
            return self.fail("recommendations_failing_under_degradation", message=summary)

        if p95_s > self.P95_SLO_S:
            print(f"ERROR: recommendation too slow under the upstream delay (p95 {p95_s:.2f}s): {summary}")
            return self.fail("recommendations_too_slow_under_degradation", message=summary, p95_s=round(p95_s, 3))

        print(f"SUCCESS: degraded but available: {summary}")

        # 4. Every answer is a real recommendation.
        catalog = set(problem.catalog_ids())
        for r in ok:
            ids = r["ids"]
            if not ids:
                print("ERROR: recommendation answered with an empty list")
                return self.fail("recommendations_empty", message="empty recommendation list under degradation")

            unknown = set(ids) - catalog
            if len(ids) > self.MAX_RECOMMENDATIONS or unknown or problem.PROBE_PRODUCT in ids:
                print(f"ERROR: invalid recommendation {ids} (unknown ids: {sorted(unknown)})")
                return self.fail("recommendations_invalid", message=f"invalid recommendation {ids}")

        # A random sample of 5 out of 9 products repeating 19 times in a row means a fixed, hard-coded list.
        if len(catalog) - 1 > self.MAX_RECOMMENDATIONS and len(ok) >= 5:
            if len({tuple(sorted(r["ids"])) for r in ok}) == 1:
                print(f"ERROR: every recommendation was identical: {ok[0]['ids']}")
                return self.fail("recommendations_constant", message=f"constant recommendation {ok[0]['ids']}")

        print("SUCCESS: recommendation is valid")

        # 5. With product-catalog healthy again, new catalog data must show up.
        try:
            problem.remove_delay()
        except RuntimeError as exc:
            print(f"ERROR: could not lift the product-catalog delay: {exc}")
            return self.fail("impairment_unavailable", message=f"could not lift the delay: {exc}")

        try:
            product_id = problem.insert_probe_product()
        except RuntimeError as exc:
            print(f"ERROR: could not insert the freshness probe product: {exc}")
            return self.fail("freshness_probe_setup_failed", message=str(exc))

        try:
            others = [i for i in problem.catalog_ids() if i != product_id]
            fresh, last = self._wait_for_new_product(product_id, others)
        finally:
            try:
                problem.delete_product(product_id)
            except Exception as exc:
                print(f"WARNING: Could not delete probe product {product_id}: {exc}")

        if not fresh:
            print(f"ERROR: new catalog data never reached recommendation within {self.FRESHNESS_WINDOW_S}s: {last}")
            return self.fail("catalog_changes_not_picked_up", message=f"last answer {last}")

        print("SUCCESS: new catalog data reached recommendation once product-catalog recovered")

        print("Mitigation accepted: recommendation degrades gracefully and refreshes when product-catalog recovers.")
        return {"success": True}

    def _wait_for_new_product(self, product_id: str, others: list[str]) -> tuple[bool, dict | None]:
        # Excluding every other product leaves exactly one valid answer
        deadline = time.monotonic() + self.FRESHNESS_WINDOW_S
        last = None

        while time.monotonic() < deadline:
            last = self.problem.probe_recommendations(1, 0, self.PROBE_DEADLINE_S, exclude_ids=others)[0]
            if last["ok"] and last["ids"] == [product_id]:
                return True, last
            time.sleep(self.FRESHNESS_POLL_S)

        return False, last

    def _incident_running(self) -> bool:
        problem = self.problem
        out = problem.kubectl.exec_command(
            f"kubectl get schedule {problem.CHAOS_NAME} -n {problem.CHAOS_NAMESPACE} -o name --ignore-not-found"
        )
        return problem.CHAOS_NAME in out

    @staticmethod
    def _p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]

    @staticmethod
    def _summary(results: list[dict]) -> str:
        ok = sum(1 for r in results if r["ok"])
        ms = sorted(r["ms"] for r in results)
        codes = sorted({r.get("code", "OK") for r in results})
        return f"{ok}/{len(results)} ok, median {ms[len(ms) // 2]:.0f} ms, max {ms[-1]:.0f} ms, codes {codes}"
