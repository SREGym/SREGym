import math
import random
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
         there and unchanged. The recommendation and product-catalog Services and the recommendation pods' name
         resolution (hostAliases, DNS) are unchanged, since the delay is built on them.
      2. Recommendation works normally when product-catalog is healthy, and still reads it. The oracle
         temporarily removes the delay, inserts a new product P1 to the catalog, and waits until recommendation
         returns P1, through the Service and from every pod of the rollout. This rejects hard-coded lists and
         fixes that drop the dependency, and it fills the in-memory caches the agent's restart cleared.
      3. After the incident's delay is applied again, recommendation keeps working despite the slowdown. At least
         90% of requests must succeed, and at least 90% must succeed within 0.25 seconds less than the delay. If a
         request is still waiting on product-catalog, it cannot return that early, so simply increasing the timeout
         fails. While the delay is active, recommendation must serve the cached catalog. The oracle excludes every
         product except P1 and a random original product X (a request it never sent while healthy), so the only
         valid answer is {P1, X}. Empty, stubbed, or made-up lists are rejected.
      4. Stale data has an age limit, and nothing gets around the impaired product-catalog. The buggy source
         states the policy: product data up to 2 minutes old is acceptable. The oracle now holds product-catalog's
         answers for 4 minutes and adds P3. Nothing that new can come back through product-catalog in that time,
         so any answer with P3 means recommendation reads the catalog some other way (an undelayed copy, the
         frontend, the database) and fails. #972 is a source-repair problem: the impairment stays. With light
         traffic flowing, the oracle waits until any data recommendation could have cached is more than 2 minutes
         old. After that, every answer must fail or be empty. Any other answer serves stale data. The late checks
         go through the client pod and through the real frontend, so a service can't treat the oracle's requests
         differently from real traffic.
      5. Once product-catalog is healthy again, new catalog data shows up within the same 2 minutes. The oracle
         inserts a product P2 and waits for it to appear. Together with steps 2 and 4, this makes
         "bounded staleness" concrete: stale data is served only while product-catalog is failing, and
         never for more than 2 minutes.

    Targeted calls go through the recommendation Service from a short-lived client pod built from the stock
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
    # The caches are refreshed right before the hold starts, so 5 s past the limit every valid cache has expired, while
    # one allowed to serve data about 2.5 minutes old still answers.
    STALENESS_MARGIN_S = 5
    FRESHNESS_WINDOW_S = MAX_STALENESS_S + 10
    # Longer than the whole staleness phase (about 185-215 s from adding P3 to the last late call), so nothing added
    # during it can come back through product-catalog. Chaos Mesh can't apply a 300 s delay here ("unable to set
    # tcs"), 240 s works.
    HOLD_DELAY_S = 240
    # Light traffic while waiting out the limit. It also catches an age measured from the last attempt instead
    # of the last success, which would never expire under traffic.
    TRAFFIC_INTERVAL_S = 10
    LATE_TARGETED_PROBES = 10
    LATE_FRONTEND_PROBES = 10
    # After product-catalog's answers were held for minutes, its TCP retransmissions back off (up to about 2 minutes
    # apart), so recommendation's existing connection can take that long to recover. The policy's 2 minutes for
    # picking up new data start after that.
    RECOVERY_WINDOW_S = FRESHNESS_WINDOW_S + 120

    # Rollout settle (60 s) + healthy window + 20 probes + the hold up to the late checks (about 3.5 min)
    # + the recovery window (up to about 4 min), with room for slow exec calls.
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
        # Pods were only added while impaired (an autoscaler, or the agent): new pods aren't held by the delay.
        "recommendation_scaled_under_degradation": FailureClass.AMBIGUOUS,
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
        self.routing_baseline: dict | None = None

    def capture_baseline(self) -> None:
        super().capture_baseline()
        error = None
        for _ in range(3):
            try:
                rows = self.problem.catalog_rows()
                routing = self.problem.routing_specs()
            except RuntimeError as exc:
                error = exc
            else:
                if rows and all(routing.values()):
                    self.catalog_baseline, self.routing_baseline = set(rows), routing
                    return
                error = RuntimeError(f"catalog rows {len(rows)}, routing {routing}")
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

        # 1. The original catalog survived, and the routing the incident is built on is unchanged.
        failure = self._catalog_intact() or self._routing_unchanged()
        if failure:
            return failure
        originals = sorted(row.split("|", 1)[0] for row in self.catalog_baseline)

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
        refreshed_at = time.monotonic()
        print(f"SUCCESS: healthy path answers and returned the new product {p1}")

        # 3. Steady impairment on the current pods, verified before measuring.
        failure = self._apply_delay()
        if failure:
            return failure

        x = random.choice(originals)
        try:
            others = [i for i in problem.catalog_ids() if i not in (p1, x)]
        except RuntimeError as exc:
            print(f"ERROR: could not read the catalog: {exc}")
            return self.fail("catalog_unavailable", message=str(exc))
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

        # 4. Stale data has an age limit, and nothing gets around the impaired product-catalog.
        failure = self._staleness_failure(added, p1, originals, deadline_s)
        if failure:
            return failure

        # 5. With product-catalog healthy again, new catalog data must show up.
        failure = self._lift_delay()
        if failure:
            return failure

        probe = self._add_probe_product(added)
        if "reason" in probe:
            return probe
        p2, others = probe["id"], probe["others"]

        fresh, last = self._wait_for_product(p2, others, window_s=self.RECOVERY_WINDOW_S)
        if not fresh:
            return self._stale_failure("after product-catalog recovered", last, window_s=self.RECOVERY_WINDOW_S)

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

    def _routing_unchanged(self) -> dict | None:
        """
        The delay selects pods by their Deployment label and the app finds them by name, so pointing a Service at
        other pods, or a name at another address, routes around it.
        """
        if not self.routing_baseline:
            print("ERROR: the Services were not recorded before injection")
            return self.fail("baseline_missing", message="capture_baseline() did not record the Services")

        try:
            current = self.problem.routing_specs()
        except RuntimeError as exc:
            print(f"ERROR: could not read the Services: {exc}")
            return self.fail("probe_unavailable", message=str(exc))

        changed = {name: current.get(name) for name, spec in self.routing_baseline.items() if current.get(name) != spec}
        if changed:
            print(f"ERROR: routing changed since injection: {changed} (was {self.routing_baseline})")
            return self.fail("upstream_bypassed", message=f"routing changed: {sorted(changed)}")
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

    def _staleness_failure(self, added: list[str], p1: str, originals: list[str], deadline_s: float) -> dict | None:
        """
        Refresh every cache, hold product-catalog's answers for HOLD_DELAY_S, add P3, and wait out the age limit with
        light traffic.
        P3 can't come back through product-catalog in that time, so seeing it means a route around the impairment.
        Once the limit has passed, every answer must fail or be empty.
        """
        problem = self.problem
        # Refresh every pod's cache right before the hold, so the age the late checks see is known within seconds.
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

        failure = self._apply_delay(latency=f"{self.HOLD_DELAY_S}s")
        if failure:
            return failure

        # Nothing from product-catalog gets through after this point, so cached data is at least this old.
        held_at = time.monotonic()
        try:
            before = problem.rollout_state()
        except RuntimeError as exc:
            print(f"ERROR: could not read the recommendation rollout: {exc}")
            return self.fail("probe_unavailable", message=str(exc))

        probe = self._add_probe_product(added)
        if "reason" in probe:
            return probe
        p3, others = probe["id"], [i for i in probe["others"] if i != p1]

        limit = self.MAX_STALENESS_S + self.STALENESS_MARGIN_S
        print(f"Waiting until cached data is over {limit}s old, with product-catalog held for {self.HOLD_DELAY_S}s...")
        while time.monotonic() < held_at + limit:
            time.sleep(max(0.0, min(self.TRAFFIC_INTERVAL_S, held_at + limit - time.monotonic())))
            seen = problem.probe_recommendations(1, 0, deadline_s, exclude_ids=others)
            failure = self._fresh_failure(seen, p3, before)
            if failure:
                return failure

        # Ask right away, then confirm the pods and the hold were still as expected while asking.
        late = problem.probe_recommendations(self.LATE_TARGETED_PROBES, 0.5, deadline_s, exclude_ids=others)
        late += problem.probe_frontend(self.LATE_FRONTEND_PROBES, 0.3, deadline_s + 5, random.choice(originals))
        failure = self._pods_changed(before)
        if failure:
            return failure
        if problem.delay_active() is not True:
            print("ERROR: the product-catalog delay is no longer active")
            return self.fail("impairment_unavailable", message="delay not active after the staleness wait")
        failure = self._late_failure(late, p3, before)
        if failure:
            return failure

        print(
            f"SUCCESS: no data older than {self.MAX_STALENESS_S}s served and none from around the delay ({self._summary(late)})"
        )
        return None

    def _fresh_failure(self, results: list[dict], p3: str, before: dict) -> dict | None:
        """
        P3 was added after product-catalog's answers were held, so it can't have come through product-catalog.
        """
        summary = self._summary(results)
        if any(r.get("code") in ("EXEC_FAILED", "NO_OUTPUT") for r in results):
            print(f"ERROR: could not probe recommendation: {summary}")
            return self.fail("probe_unavailable", message=summary)

        leaked = [r["ids"] for r in results if r["ok"] and p3 in r["ids"]]
        if not leaked:
            return None
        # A new or restarted pod isn't held by the delay, so it could have fetched P3 legitimately.
        failure = self._pods_changed(before)
        if failure:
            return failure
        print(
            f"ERROR: recommendation served {p3}, added while product-catalog was held for {self.HOLD_DELAY_S}s, "
            f"so it reads the catalog around product-catalog: {leaked[:3]}"
        )
        return self.fail("upstream_bypassed", message=f"served {p3}, added while product-catalog was held")

    def _late_failure(self, results: list[dict], p3: str, before: dict) -> dict | None:
        """
        After the age limit, every answer must fail or be empty. Anything else serves data that is too old.
        """
        failure = self._fresh_failure(results, p3, before)
        if failure:
            return failure

        stale = [r["ids"] for r in results if r["ok"] and r["ids"]]
        if stale:
            print(
                f"ERROR: {len(stale)} of {len(results)} answers after the age limit still served cached data, "
                f"older than {self.MAX_STALENESS_S}s: {stale[:3]}"
            )
            return self.fail(
                "stale_data_served_past_limit", message=f"{len(stale)} of {len(results)} late answers were stale"
            )
        return None

    def _pods_changed(self, before: dict) -> dict | None:
        """
        A restarted or replaced pod lost its cache and isn't held by the delay, so the staleness checks would mean
        nothing for it. Pods that were only added (an autoscaler, say) can't be judged either way.
        """
        try:
            after = self.problem.rollout_state()
        except RuntimeError as exc:
            print(f"ERROR: could not read the recommendation rollout: {exc}")
            return self.fail("probe_unavailable", message=str(exc))

        restarted = [uid for uid, restarts in before.items() if after.get(uid) != restarts]
        if restarted:
            print(
                f"ERROR: recommendation pods restarted or went away while product-catalog was impaired: {before} -> {after}"
            )
            return self.fail("recommendation_restarted_under_degradation", message=f"{before} -> {after}")
        if set(after) - set(before):
            print(f"ERROR: recommendation pods were added while product-catalog was impaired: {before} -> {after}")
            return self.fail("recommendation_scaled_under_degradation", message=f"{before} -> {after}")
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

    def _wait_for_product(
        self, product_id: str, others: list[str], window_s: float | None = None
    ) -> tuple[bool, dict | None]:
        # Excluding every other product leaves exactly one valid answer
        deadline = time.monotonic() + (window_s or self.FRESHNESS_WINDOW_S)
        last = None

        while time.monotonic() < deadline:
            last = self.problem.probe_recommendations(1, 0, self.HEALTHY_DEADLINE_S, exclude_ids=others)[0]
            if last["ok"] and last["ids"] == [product_id]:
                return True, last
            time.sleep(self.FRESHNESS_POLL_S)

        return False, last

    def _stale_failure(self, when: str, last: dict | None, window_s: float | None = None) -> dict:
        if last is not None and last.get("code") in ("EXEC_FAILED", "NO_OUTPUT"):
            print(f"ERROR: could not probe recommendation: {last}")
            return self.fail("probe_unavailable", message=str(last))

        if last is None or not last["ok"]:
            print(f"ERROR: recommendation fails {when}: {last}")
            return self.fail("recommendations_failing_when_healthy", message=f"last answer {last}")

        print(
            f"ERROR: new catalog data never reached recommendation within {window_s or self.FRESHNESS_WINDOW_S}s "
            f"{when}: {last}"
        )
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
