import math
import time

from sregym.conductor.oracles.failure import FailureClass
from sregym.conductor.oracles.mitigation import MitigationOracle


class ProbeUnavailable(RuntimeError):
    """The probe itself could not run (no client pod, exec failed), so nothing is known about recommendation."""


class MissingStaleCacheFallbackMitigationOracle(MitigationOracle):
    """
    Mitigation oracle for the missing stale-cache fallback problem.

    The incident is a product-catalog slowdown that recommendation cannot survive. Since the slowdown
    is part of the incident, not something the agent is expected to remove, the oracle keeps it in
    place while evaluating the fix, and checks that recommendation now degrades gracefully.

    A mitigation only passes if all of the following are true:

      1. The application stays healthy. No deployment deleted, scaled to 0, or left with unready pods
         (inherited from MitigationOracle). Every product that was in the catalog before injection is still
         there and unchanged, and the recommendation and product-catalog Services still select the same pods.
      2. Recommendation works normally when product-catalog is healthy, and still reads it. The oracle
         temporarily removes the delay, inserts a new product P1 to the catalog, and waits until recommendation
         returns P1, through the Service and from every pod of the rollout. This rejects hard-coded lists and
         fixes that drop the dependency, and it fills the in-memory caches the agent's restart cleared.
      3. Fresh data only arrives through the impaired product-catalog. The oracle applies a much longer delay
         (60 seconds), inserts P4, and probes for under a minute. Data that new cannot come back through the
         delayed path in time, so an answer with P4 means recommendation reads the catalog some other way (an
         undelayed copy, the frontend, the database). #972 is a source-repair problem: the impairment stays.
      4. After the incident's delay is applied again, recommendation keeps working despite the slowdown. At least
         90% of requests must succeed, and at least 90% must succeed within 0.25 seconds less than the delay. If a
         request is still waiting on product-catalog, it cannot return that early, so simply increasing the timeout
         fails. While the delay is active, recommendation must serve the cached catalog. The oracle excludes every
         product except P1 and one original product X (a request it never sent while healthy), so the only valid
         answer is {P1, X}. Empty, stubbed, or made-up lists are rejected.
      5. Stale data has an age limit. The buggy source states the policy: product data up to 2 minutes old is
         acceptable. With product-catalog still impaired, the oracle adds P3, keeps light traffic flowing, and
         waits until any data recommendation could have cached is more than 2 minutes old. Answers must then
         either fail, be empty, or come from fresh data (include P3). Still serving the old list fails, both for
         the oracle's targeted requests and for ordinary ones that exclude a single product.
      6. Once product-catalog is healthy again, new catalog data shows up within the same 2 minutes. The oracle
         inserts a product P2 and waits for it to appear. Together with steps 2 and 5, this makes
         "bounded staleness" concrete: stale data is served only while product-catalog is failing, and
         never for more than 2 minutes.

    Every call goes through the recommendation Service from a short-lived client pod built from the stock
    recommendation image, the way the frontend reaches it. So a broken Service, a stray pod behind it, or
    changes inside the recommendation container can't fake an answer.

    The oracle restores the environment before it exits. Probe products and the client pod are deleted, and if
    the keep-alive delay was running when grading started, it is restored.
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
    FRESHNESS_POLL_S = 2.0
    # The freshness policy stated in the buggy source (2 minutes). The reference fix uses the same value.
    MAX_STALENESS_S = 120
    STALENESS_MARGIN_S = 20
    FRESHNESS_WINDOW_S = MAX_STALENESS_S + 10
    # Light traffic while waiting out the limit, so an age measured from the last attempt (not the last success)
    # never expires and gets caught.
    TRAFFIC_INTERVAL_S = 20
    EXPIRY_PROBES = 3
    # Ordinary requests excluding one product: a fresh catalog shows P3 in about 2 of 5 answers, so 10 or more OK
    # answers without it mean the old list is still being served.
    ORDINARY_EXPIRY_PROBES = 15
    MIN_ORDINARY_OK = 10
    # Longer than the whole bypass probe window, so fresh data cannot come back through the delayed path in time.
    BYPASS_DELAY_S = 60
    BYPASS_PROBES = 8

    # Rollout settle (60 s) + healthy window + bypass check + 20 probes + waiting out the 2 minute limit
    # + the recovery window, with room for slow exec calls.
    evaluation_timeout_seconds = 900

    FAILURE_CLASSES = {
        "recommendations_failing_when_healthy": FailureClass.AGENT_ERROR,
        "recommendations_failing_under_degradation": FailureClass.AGENT_ERROR,
        "recommendations_too_slow_under_degradation": FailureClass.AGENT_ERROR,
        "recommendations_empty": FailureClass.AGENT_ERROR,
        "recommendations_invalid": FailureClass.AGENT_ERROR,
        "catalog_changes_not_picked_up": FailureClass.AGENT_ERROR,
        "stale_data_served_past_limit": FailureClass.AGENT_ERROR,
        "catalog_data_lost": FailureClass.AGENT_ERROR,
        "upstream_bypassed": FailureClass.AGENT_ERROR,
        "recommendation_restarted_under_degradation": FailureClass.AGENT_ERROR,
        # product-catalog or Postgres fails with every pod Ready. The agent may have changed it, or the cluster did.
        "upstream_unhealthy": FailureClass.AMBIGUOUS,
        "catalog_unavailable": FailureClass.AMBIGUOUS,
        "impairment_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "probe_unavailable": FailureClass.ENVIRONMENT_ERROR,
        "catalog_probe_setup_failed": FailureClass.ENVIRONMENT_ERROR,
        "baseline_missing": FailureClass.ENVIRONMENT_ERROR,
    }

    def __init__(self, problem):
        super().__init__(problem)
        # Filled by capture_baseline() once the app is deployed, before injection.
        self.catalog_baseline: set[str] | None = None
        self.service_baseline: dict | None = None

    def capture_baseline(self) -> None:
        super().capture_baseline()
        error = None
        for _ in range(3):
            try:
                rows = self.problem.catalog_rows()
                services = self.problem.service_specs()
            except RuntimeError as exc:
                error = exc
            else:
                if rows and all(services.values()):
                    self.catalog_baseline, self.service_baseline = set(rows), services
                    return
                error = RuntimeError(f"catalog rows {len(rows)}, Services {services}")
            time.sleep(5)
        # Raising fails the deployment, which gets retried, instead of an unscored verdict after the agent's run.
        raise RuntimeError(f"could not record the catalog and Services before injection: {error}")

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
            try:
                problem.release_client()
            except Exception as exc:
                print(f"WARNING: Could not delete the probe client pod: {exc}")

    def _evaluate_behaviour(self, added: list[str]) -> dict:
        problem = self.problem
        delay_s = problem.delay_seconds
        deadline_s = delay_s + self.PROBE_DEADLINE_MARGIN_S
        fast_limit_s = delay_s - self.FAST_MARGIN_S

        # 1. The original catalog survived, and the Services the incident is built on are unchanged.
        failure = self._catalog_intact() or self._services_unchanged()
        if failure:
            return failure

        # 2. Healthy window: lift any impairment, then the service must return a product added just now.
        failure = self._lift_delay()
        if failure:
            return failure

        try:
            problem.wait_until_answering()
        except ProbeUnavailable as exc:
            print(f"ERROR: could not probe recommendation: {exc}")
            return self.fail("probe_unavailable", message=str(exc))
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

        failure = self._warm_rollout(p1, others)
        if failure:
            return failure
        print(f"SUCCESS: healthy path answers and returned the new product {p1}")

        # 3. Fresh data must not reach recommendation around the impaired product-catalog.
        failure = self._bypass_failure(added, p1, deadline_s)
        if failure:
            return failure

        # Lift the long delay and refresh every pod again, so the degraded window starts from a fresh cache
        # instead of depending on background traffic.
        failure = self._lift_delay()
        if failure:
            return failure
        try:
            others = [i for i in problem.catalog_ids() if i != p1]
        except RuntimeError as exc:
            print(f"ERROR: could not read the catalog: {exc}")
            return self.fail("catalog_unavailable", message=str(exc))
        failure = self._warm_rollout(p1, others)
        if failure:
            return failure
        refreshed_at = time.monotonic()

        # 4. Steady impairment on the current pods, verified before measuring.
        failure = self._apply_delay()
        if failure:
            return failure

        # No fetch from product-catalog can succeed after this point, so cached data is at least this old.
        degraded_at = time.monotonic()
        try:
            before = problem.rollout_state()
            x = min(row.split("|", 1)[0] for row in self.catalog_baseline)
            others = [i for i in problem.catalog_ids() if i not in (p1, x)]
        except RuntimeError as exc:
            print(f"ERROR: could not read the rollout or the catalog: {exc}")
            return self.fail("probe_unavailable", message=str(exc))
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

        # Every answer is the cached catalog data, which after the exclusions is exactly {P1, X}.
        for r in ok:
            if not r["ids"]:
                print("ERROR: recommendation answered with an empty list under the upstream delay")
                return self.fail("recommendations_empty", message="empty recommendation list under degradation")
            if set(r["ids"]) != {p1, x}:
                print(
                    f"ERROR: recommendation answered {r['ids']} under the upstream delay, expected the cached {p1}, {x}"
                )
                return self.fail("recommendations_invalid", message=f"answered {r['ids']}, expected {p1} and {x}")

        print("SUCCESS: answers under the delay come from the cached catalog")

        # 5. Stale data has an age limit.
        failure = self._staleness_failure(added, p1, x, degraded_at, before, deadline_s)
        if failure:
            return failure

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

    def _catalog_intact(self) -> dict | None:
        if not self.catalog_baseline:
            print("ERROR: the original catalog was not recorded before injection")
            return self.fail("baseline_missing", message="capture_baseline() did not record the catalog")

        try:
            current = set(self.problem.catalog_rows())
        except RuntimeError as exc:
            print(f"ERROR: could not read the catalog: {exc}")
            return self.fail("catalog_unavailable", message=str(exc))

        missing = sorted(row.split("|", 1)[0] for row in self.catalog_baseline - current)
        if missing:
            print(
                f"ERROR: {len(missing)} of {len(self.catalog_baseline)} original products are missing or changed: {missing}"
            )
            return self.fail(
                "catalog_data_lost", message=f"{len(missing)} original products missing or changed", ids=missing
            )

        print(f"SUCCESS: all {len(self.catalog_baseline)} original products are intact")
        return None

    def _services_unchanged(self) -> dict | None:
        """
        The delay selects pods by their Deployment label, so pointing a Service at other pods routes around it.
        """
        if not self.service_baseline:
            print("ERROR: the Services were not recorded before injection")
            return self.fail("baseline_missing", message="capture_baseline() did not record the Services")

        try:
            current = self.problem.service_specs()
        except RuntimeError as exc:
            print(f"ERROR: could not read the Services: {exc}")
            return self.fail("probe_unavailable", message=str(exc))

        changed = {name: current.get(name) for name, spec in self.service_baseline.items() if current.get(name) != spec}
        if changed:
            print(f"ERROR: Services changed since injection: {changed} (were {self.service_baseline})")
            return self.fail("upstream_bypassed", message=f"Services changed: {sorted(changed)}")
        return None

    def _apply_delay(self, latency: str | None = None) -> dict | None:
        problem = self.problem
        try:
            problem.apply_delay(keep_alive=False, latency=latency)
        except RuntimeError as exc:
            print(f"ERROR: could not apply the product-catalog delay: {exc}")
            return self.fail("impairment_unavailable", message=str(exc))

        if not problem.wait_for_delay(active=True):
            print("ERROR: could not apply the product-catalog delay")
            return self.fail("impairment_unavailable", message=f"{latency or problem.delay} delay never became active")
        return None

    def _lift_delay(self) -> dict | None:
        """
        Remove the delay. remove_delay() also proves a direct ListProducts call is fast and OK again.
        """
        try:
            self.problem.remove_delay()
            return None
        except RuntimeError as exc:
            error = str(exc)

        upstream = self.problem.measure_upstream()
        if upstream is None:
            print(f"ERROR: could not time product-catalog: {error}")
            return self.fail("probe_unavailable", message=f"upstream timing failed: {error}")

        if upstream["s"] >= self.problem.delay_active_min_s:
            print(f"ERROR: could not lift the product-catalog delay: {error}")
            return self.fail("impairment_unavailable", message=f"could not lift the delay: {error}")

        if upstream["code"] == "OK":
            print(f"WARNING: product-catalog was slow to recover but answers now: {upstream}")
            return None

        print(f"ERROR: product-catalog fails with the delay lifted: {upstream}")
        return self.fail("upstream_unhealthy", message=f"ListProducts answered {upstream['code']}")

    def _warm_rollout(self, p1: str, others: list[str]) -> dict | None:
        """
        Every pod of the rollout must return P1 too, so each one has the new catalog cached before the delay.
        """
        problem = self.problem
        try:
            pods = problem.rollout_pods()
        except RuntimeError as exc:
            print(f"ERROR: could not list the recommendation pods: {exc}")
            return self.fail("probe_unavailable", message=str(exc))

        for pod in pods:
            deadline = time.monotonic() + self.FRESHNESS_WINDOW_S
            last = None
            while time.monotonic() < deadline:
                last = problem.probe_recommendations(1, 0, self.HEALTHY_DEADLINE_S, exclude_ids=others, pod=pod)[0]
                if last["ok"] and last["ids"] == [p1]:
                    break
                time.sleep(self.FRESHNESS_POLL_S)
            else:
                return self._stale_failure(f"on pod {pod} with product-catalog healthy", last)
        return None

    def _bypass_failure(self, added: list[str], p1: str, deadline_s: float) -> dict | None:
        """
        Under a delay much longer than the probe window, data added after the delay started cannot come back from
        product-catalog in time. Seeing it means recommendation got it some other way.
        """
        problem = self.problem
        failure = self._apply_delay(latency=f"{self.BYPASS_DELAY_S}s")
        if failure:
            return failure

        try:
            p4 = problem.new_product_id()
            added.append(p4)
            problem.insert_probe_product(p4)
            others = [i for i in problem.catalog_ids() if i not in (p1, p4)]
        except RuntimeError as exc:
            print(f"ERROR: could not add a probe product to the catalog: {exc}")
            return self.fail("catalog_probe_setup_failed", message=str(exc))

        results = problem.probe_recommendations(self.BYPASS_PROBES, 0.5, deadline_s, exclude_ids=others)
        if any(r.get("code") in ("EXEC_FAILED", "NO_OUTPUT") for r in results):
            print(f"ERROR: could not probe recommendation: {self._summary(results)}")
            return self.fail("probe_unavailable", message=self._summary(results))

        leaked = [r["ids"] for r in results if r["ok"] and p4 in r["ids"]]
        if leaked:
            print(
                f"ERROR: recommendation served {p4}, added after a {self.BYPASS_DELAY_S}s product-catalog delay "
                f"started, so it reads the catalog around product-catalog: {leaked}"
            )
            return self.fail(
                "upstream_bypassed", message=f"fresh product {p4} served under a {self.BYPASS_DELAY_S}s delay"
            )

        print(f"SUCCESS: no fresh data arrived around the delayed product-catalog ({self._summary(results)})")
        return None

    def _staleness_failure(
        self, added: list[str], p1: str, x: str, degraded_at: float, before: dict, deadline_s: float
    ) -> dict | None:
        problem = self.problem
        probe = self._add_probe_product(added)
        if "reason" in probe:
            return probe
        p3, others = probe["id"], [i for i in probe["others"] if i != p1]

        limit = self.MAX_STALENESS_S + self.STALENESS_MARGIN_S
        print(f"Waiting until cached data is over {limit}s old, with product-catalog still impaired...")
        while time.monotonic() < degraded_at + limit:
            time.sleep(max(0.0, min(self.TRAFFIC_INTERVAL_S, degraded_at + limit - time.monotonic())))
            problem.probe_recommendations(1, 0, deadline_s, exclude_ids=[x])

        try:
            after = problem.rollout_state()
        except RuntimeError as exc:
            print(f"ERROR: could not read the recommendation rollout: {exc}")
            return self.fail("probe_unavailable", message=str(exc))
        if after != before:
            # A restarted pod lost its cache and isn't covered by the delay, so the staleness check would mean nothing.
            print(
                f"ERROR: recommendation pods restarted or changed while product-catalog was impaired: {before} -> {after}"
            )
            return self.fail("recommendation_restarted_under_degradation", message=f"{before} -> {after}")
        if problem.delay_active() is not True:
            print("ERROR: the product-catalog delay is no longer active")
            return self.fail("impairment_unavailable", message="delay not active after the staleness wait")

        late = problem.probe_recommendations(self.EXPIRY_PROBES, 1.0, deadline_s, exclude_ids=others)
        failure = self._expiry_failure(late, p1, p3)
        if failure:
            return failure

        ordinary = problem.probe_recommendations(self.ORDINARY_EXPIRY_PROBES, 0.3, deadline_s, exclude_ids=[x])
        failure = self._ordinary_expiry_failure(ordinary, x, p3)
        if failure:
            return failure

        print(f"SUCCESS: no data older than {self.MAX_STALENESS_S}s served ({self._summary(late + ordinary)})")
        return None

    def _expiry_failure(self, results: list[dict], p1: str, p3: str) -> dict | None:
        """
        After the age limit, an answer may fail, be empty, or come from fresh data. It may not be the old [P1].
        """
        summary = self._summary(results)
        if any(r.get("code") in ("EXEC_FAILED", "NO_OUTPUT") for r in results):
            print(f"ERROR: could not probe recommendation: {summary}")
            return self.fail("probe_unavailable", message=summary)

        for r in results:
            if not r["ok"]:
                continue
            ids = set(r["ids"])
            if ids == {p1}:
                print(f"ERROR: recommendation still serves data cached over {self.MAX_STALENESS_S}s ago: {r['ids']}")
                return self.fail("stale_data_served_past_limit", message=f"answered {r['ids']} without {p3}")
            if not ids <= {p1, p3}:
                print(f"ERROR: recommendation answered {r['ids']} after the cache expired")
                return self.fail("recommendations_invalid", message=f"answered {r['ids']} after the cache expired")
        return None

    def _ordinary_expiry_failure(self, results: list[dict], x: str, p3: str) -> dict | None:
        """
        The same check with requests that look like the frontend's (excluding one product), so a service can't
        treat the oracle's requests differently from real traffic.
        """
        summary = self._summary(results)
        if any(r.get("code") in ("EXEC_FAILED", "NO_OUTPUT") for r in results):
            print(f"ERROR: could not probe recommendation: {summary}")
            return self.fail("probe_unavailable", message=summary)

        # Empty answers carry no stale data, so only answers with products count.
        ok = [r for r in results if r["ok"] and r["ids"]]
        if any(x in r["ids"] or len(r["ids"]) > 5 for r in ok):
            print(f"ERROR: recommendation answered with the excluded product or too many products: {summary}")
            return self.fail("recommendations_invalid", message=f"invalid answers after the cache expired: {summary}")
        if len(ok) >= self.MIN_ORDINARY_OK and not any(p3 in r["ids"] for r in ok):
            print(f"ERROR: {len(ok)} ordinary answers after the age limit and none includes {p3}: {summary}")
            return self.fail("stale_data_served_past_limit", message=f"{len(ok)} ordinary answers without {p3}")
        return None

    def _add_probe_product(self, added: list[str]) -> dict:
        problem = self.problem

        try:
            # Remember the id before inserting, so a timeout after the commit still gets it cleaned up.
            product_id = problem.new_product_id()
            added.append(product_id)
            problem.insert_probe_product(product_id)
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
