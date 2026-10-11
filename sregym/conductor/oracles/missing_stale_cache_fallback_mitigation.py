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
      2. Recommendation works normally when product-catalog is healthy, and still reads it. The oracle
         temporarily removes the delay, inserts a new product P1 to the catalog, and waits until recommendation
         returns P1. This rejects hard-coded lists and fixes that drop the dependency, and it fills the
         in-memory cache the agent's restart cleared.
      3. After the delay is applied again to the current pods, recommendation keeps working despite
         the slowdown. At least 90% of requests must succeed, and at least 90% must succeed within 0.25 seconds
         less than the delay. If a request is still waiting on product-catalog, it cannot return that early, so
         simply increasing the timeout fails.
      4. While the delay is active, recommendation must serve the cached catalog. The oracle excludes every product
         except P1, so the only valid answer is [P1]. Empty, stubbed, or made-up lists are rejected.
      5. The configured upstream is still the delayed product-catalog. Pointing recommendation at an
         undelayed copy of product-catalog is just working around the benchmark, not fixing it.
      6. Once product-catalog is healthy again, new catalog data shows up within 60 seconds. The oracle
         inserts a second product P2 and waits for it to appear. Together with step 2, this makes
         "bounded staleness" concrete. In essence, stale data is allowed only while product-catalog is failing.

    The oracle restores the environment before it exits. Probe products are deleted, and if the keep-alive
    delay was running when grading started, it is restored.
    """

    PROBE_COUNT = 20
    PROBE_INTERVAL_S = 0.2
    MIN_SUCCESS_RATE = 0.9
    # Nothing that waited for product-catalog can answer before the delay, which holds every response packet.

    # We count how many requests finish quickly instead of using p95, since an occasional slow call is expected
    # under background load. In testing, a fallback after a 2 s timeout gave p95 latencies around 2.46–2.71 s,
    # with a few requests taking as long as 2.8 s.
    MIN_FAST_RATE = 0.9
    FAST_MARGIN_S = 0.25
    # Lets a slow but successful answer (no fallback, huge timeout) count as slow rather than failed.
    PROBE_DEADLINE_MARGIN_S = 2.0
    HEALTHY_DEADLINE_S = 5.0
    FRESHNESS_WINDOW_S = 60
    FRESHNESS_POLL_S = 2.0

    # Rollout settle (60 s) + lifting/applying the delay three times + two freshness windows + 20 probes.
    evaluation_timeout_seconds = 900

    FAILURE_CLASSES = {
        "recommendations_failing_when_healthy": FailureClass.AGENT_ERROR,
        "recommendations_failing_under_degradation": FailureClass.AGENT_ERROR,
        "recommendations_too_slow_under_degradation": FailureClass.AGENT_ERROR,
        "recommendations_empty": FailureClass.AGENT_ERROR,
        "recommendations_invalid": FailureClass.AGENT_ERROR,
        "catalog_changes_not_picked_up": FailureClass.AGENT_ERROR,
        "upstream_bypassed": FailureClass.AGENT_ERROR,
        # product-catalog errors with every pod Ready. The agent may have changed it, or the cluster did.
        "upstream_unhealthy": FailureClass.AMBIGUOUS,
        "impairment_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "probe_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "catalog_probe_setup_failed": FailureClass.ENVIRONMENT_ERROR,
    }

    def evaluate(self) -> dict:
        print("=== Mitigation Evaluation (recommendation stale-cache fallback) ===")

        base = super().evaluate()
        if not base.get("success"):
            return base

        problem = self.problem
        incident_was_running = problem.incident_running()
        added: list[str] = []
        try:
            return self._evaluate_behaviour(added)
        finally:
            for product_id in added:
                try:
                    problem.delete_product(product_id)
                except Exception as exc:
                    print(f"WARNING: Could not delete probe product {product_id}: {exc}")
            try:
                if incident_was_running:
                    problem.apply_delay(keep_alive=True)
                else:
                    problem.remove_delay()
            except Exception as exc:
                print(f"WARNING: Could not restore the upstream impairment state: {exc}")

    def _evaluate_behaviour(self, added: list[str]) -> dict:
        problem = self.problem
        delay_s = problem.delay_seconds
        deadline_s = delay_s + self.PROBE_DEADLINE_MARGIN_S
        fast_limit_s = delay_s - self.FAST_MARGIN_S

        # 2. Healthy window: lift any impairment, then the service must return a product added just now.
        failure = self._lift_delay()
        if failure:
            return failure

        try:
            problem.wait_until_answering()
        except RuntimeError as exc:
            print(f"ERROR: recommendation does not answer even with product-catalog healthy: {exc}")
            return self.fail("recommendations_failing_when_healthy", message=str(exc))

        probe = self._add_probe_product(added)
        if "reason" in probe:
            return probe
        p1, others = probe["id"], probe["others"]

        fresh, last = self._wait_for_product(p1, others)
        if not fresh:
            return self._stale_failure("with product-catalog healthy", last)
        refreshed_at = time.monotonic()
        print(f"SUCCESS: healthy path answers and returned the new product {p1}")

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

        results = problem.probe_recommendations(self.PROBE_COUNT, self.PROBE_INTERVAL_S, deadline_s, exclude_ids=others)
        stale_s = time.monotonic() - refreshed_at
        ok = [r for r in results if r["ok"]]
        success_rate = len(ok) / len(results)
        fast = [r for r in ok if r["ms"] / 1000 <= fast_limit_s]
        fast_rate = len(fast) / len(results)
        p95_s = self._p95([r["ms"] for r in results]) / 1000
        summary = self._summary(results)

        if any(r.get("code") in ("EXEC_FAILED", "NO_OUTPUT") for r in results):
            # The probe itself could not run (kubectl exec failed).
            print(f"ERROR: could not probe recommendation: {summary}")
            return self.fail("probe_unavailable", message=summary)

        if success_rate < self.MIN_SUCCESS_RATE:
            print(f"ERROR: recommendation fails under the upstream delay: {summary}")
            return self.fail("recommendations_failing_under_degradation", message=summary)

        within = f"{len(fast)}/{len(results)} within {fast_limit_s:.2f}s, p95 {p95_s:.2f}s"
        if fast_rate < self.MIN_FAST_RATE:
            print(f"ERROR: recommendation too slow under the upstream delay ({within}): {summary}")
            return self.fail(
                "recommendations_too_slow_under_degradation",
                message=summary,
                fast_rate=fast_rate,
                limit_s=fast_limit_s,
                p95_s=round(p95_s, 3),
            )

        print(f"SUCCESS: degraded but available ({within}, data up to {stale_s:.0f}s old): {summary}")

        # 4. Every answer is the cached catalog data, which after the exclusions is exactly [P1].
        for r in ok:
            if not r["ids"]:
                print("ERROR: recommendation answered with an empty list under the upstream delay")
                return self.fail("recommendations_empty", message="empty recommendation list under degradation")
            if r["ids"] != [p1]:
                print(f"ERROR: recommendation answered {r['ids']} under the upstream delay, expected the cached [{p1}]")
                return self.fail("recommendations_invalid", message=f"answered {r['ids']}, expected [{p1}]")

        print("SUCCESS: answers under the delay come from the cached catalog")

        # 5. Recommendation still calls the delayed product-catalog.
        configured = problem.measure_upstream()
        if configured is None:
            print("ERROR: could not time recommendation's configured product-catalog address")
            return self.fail("probe_unavailable", message="upstream timing failed")
        if configured["code"] == "OK" and configured["s"] < problem.delay_active_min_s:
            print(f"ERROR: recommendation calls an undelayed upstream {configured['addr']} ({configured['s']:.2f}s)")
            return self.fail("upstream_bypassed", message=f"configured upstream {configured['addr']} is not delayed")

        # 6. With product-catalog healthy again, new catalog data must show up.
        failure = self._lift_delay()
        if failure:
            return failure

        probe = self._add_probe_product(added)
        if "reason" in probe:
            return probe
        p2, others = probe["id"], probe["others"]

        fresh, last = self._wait_for_product(p2, others)
        if not fresh:
            return self._stale_failure("after product-catalog recovered", last)

        print("SUCCESS: new catalog data reached recommendation once product-catalog recovered")

        print("Mitigation accepted: recommendation degrades gracefully and refreshes when product-catalog recovers.")
        return {"success": True}

    def _lift_delay(self) -> dict | None:
        """
        Remove the delay. remove_delay() also proves a direct ListProducts call is fast and OK again.
        """
        try:
            self.problem.remove_delay()
            return None
        except RuntimeError as exc:
            error = str(exc)

        upstream = self.problem.measure_upstream(self.problem.UPSTREAM_ADDR)
        if upstream is None:
            print(f"ERROR: could not time product-catalog: {error}")
            return self.fail("probe_unavailable", message=f"upstream timing failed: {error}")

        if upstream["s"] >= self.problem.delay_active_min_s:
            print(f"ERROR: could not lift the product-catalog delay: {error}")
            return self.fail("impairment_unavailable", message=f"could not lift the delay: {error}")

        print(f"ERROR: product-catalog fails with the delay lifted: {upstream}")
        return self.fail("upstream_unhealthy", message=f"ListProducts answered {upstream['code']}")

    def _add_probe_product(self, added: list[str]) -> dict:
        problem = self.problem

        try:
            product_id = problem.insert_probe_product()
            added.append(product_id)
            others = [i for i in problem.catalog_ids() if i != product_id]
        except RuntimeError as exc:
            print(f"ERROR: could not add a probe product to the catalog: {exc}")
            return self.fail("catalog_probe_setup_failed", message=str(exc))

        return {"id": product_id, "others": others}

    def _wait_for_product(self, product_id: str, others: list[str]) -> tuple[bool, dict | None]:
        # Excluding every other product leaves exactly one valid answer
        deadline = time.monotonic() + self.FRESHNESS_WINDOW_S
        last = None

        while time.monotonic() < deadline:
            last = self.problem.probe_recommendations(1, 0, self.HEALTHY_DEADLINE_S, exclude_ids=others)[0]
            if last["ok"] and last["ids"] == [product_id]:
                return True, last
            time.sleep(self.FRESHNESS_POLL_S)

        return False, last

    def _stale_failure(self, when: str, last: dict | None) -> dict:
        if last is not None and last.get("code") in ("EXEC_FAILED", "NO_OUTPUT"):
            print(f"ERROR: could not probe recommendation: {last}")
            return self.fail("probe_unavailable", message=str(last))

        if last is None or not last["ok"]:
            print(f"ERROR: recommendation fails {when}: {last}")
            return self.fail("recommendations_failing_when_healthy", message=f"last answer {last}")

        print(f"ERROR: new catalog data never reached recommendation within {self.FRESHNESS_WINDOW_S}s {when}: {last}")
        return self.fail("catalog_changes_not_picked_up", message=f"last answer {last}")

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
