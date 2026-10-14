import random
from unittest.mock import Mock

import pytest

import sregym.conductor.oracles.missing_stale_cache_fallback_mitigation as oracle_module
from sregym.conductor.oracles.missing_stale_cache_fallback_mitigation import (
    MissingStaleCacheFallbackMitigationOracle,
    ProbeUnavailable,
)
from sregym.conductor.oracles.mitigation import MitigationOracle

CATALOG = [
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "HQTGWGPNH4",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
]
ROUTING = {
    "recommendation": {
        "selector": {"opentelemetry.io/name": "recommendation"},
        "ports": [(8080, "8080", "TCP")],
        "session_affinity": "None",
    },
    "product-catalog": {
        "selector": {"opentelemetry.io/name": "product-catalog"},
        "ports": [(8080, "8080", "TCP")],
        "session_affinity": "None",
    },
    "recommendation-dns": {"host_aliases": None, "dns_policy": "ClusterFirst", "dns_config": None},
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeShop:
    """Recommendation pods behind a round-robin Service, product-catalog, and the agent's fix chosen per test"""

    def __init__(self, delay_s=3.0, degraded="cache", degraded_ms=1100.0, max_age_s=120.0, replicas=1):
        self.clock = Clock()
        self.delay = f"{delay_s:g}s"
        self.delay_seconds = delay_s
        self.delay_active_min_s = 0.8 * delay_s
        self.catalog = list(CATALOG)
        self.pods = [f"recommendation-{i}" for i in range(replicas)]
        self.caches = {pod: (list(CATALOG), 0.0) for pod in self.pods}
        self._next_pod = 0
        self.routing = {name: dict(spec) for name, spec in ROUTING.items()}
        self.schedule = True
        # Latency of the current delay in seconds, 0 while product-catalog is healthy.
        self.latency = delay_s
        # What the fixed service does when product-catalog is delayed: cache, error, wait, hardcoded, empty.
        self.degraded = degraded
        self.degraded_ms = degraded_ms
        # How long the cache may be served while product-catalog fails. None means forever.
        self.max_age_s = max_age_s
        # What the service answers once the cache is too old: error or empty.
        self.after_expiry = "error"
        # Refreshes in the background through product-catalog with this timeout (works while the delay is shorter).
        self.background_timeout_s = None
        # Reads an undelayed product-catalog copy that serves the same database.
        self.copy_upstream = False
        # Once the cache is too old, reads the catalog some other way (the frontend) instead of failing.
        self.bypass_after_expiry = False
        # A background thread that reads the catalog some other way every n seconds. None means no poller.
        self.poller_period_s = None
        self._polled_at = 0.0
        # After the age limit, still serves the old cache to every n-th request. 0 means never.
        self.leak_every = 0
        self._late = 0
        # Applies the age limit only to the oracle's own client, not to requests coming from the frontend.
        self.fingerprint = False
        self.healthy_ok = True
        # Every n-th answer from the cache is 1.5 s slower (background load). 0 means never.
        self.tail_every = 0
        self._served = 0
        self.refreshes = True
        self.stuck_after_fallback = False
        self.fell_back = False
        self.can_lift = True
        self.can_apply = True
        self.upstream_code = "OK"
        self.exec_broken = False
        self.insert_broken = False
        self.restart_at = None
        self.scale_at = None
        self.restored = []
        self.client_released = 0
        self.pod_probes = []
        self._random = random.Random(7)
        self._next = 0

    # The problem API the oracle uses

    def incident_running(self):
        return self.schedule

    def remove_delay(self):
        self.schedule = False
        if not self.can_lift:
            raise RuntimeError("product-catalog did not answer quickly after removing the delay")
        self.latency = 0
        if self.upstream_code != "OK":
            raise RuntimeError("product-catalog did not answer quickly after removing the delay")

    def apply_delay(self, keep_alive, latency=None):
        self.schedule = keep_alive
        self.latency = float((latency or self.delay)[:-1]) if self.can_apply else 0
        self.restored.append(keep_alive)

    def wait_for_delay(self, active):
        return (self.latency > 0) is active

    def delay_active(self):
        return self.latency > 0

    def wait_until_answering(self):
        if self.exec_broken:
            raise ProbeUnavailable("could not probe recommendation")
        if not self.healthy_ok:
            raise RuntimeError("Recommendation is not answering")

    def catalog_ids(self):
        return sorted(self.catalog)

    def catalog_rows(self):
        return [f"{i}|Product {i}|19" for i in sorted(self.catalog)]

    def routing_specs(self):
        return {name: dict(spec) for name, spec in self.routing.items()}

    def rollout_pods(self):
        return list(self.pods)

    def rollout_state(self):
        restarted = self.restart_at is not None and self.clock.now >= self.restart_at
        state = {pod: int(restarted) for pod in self.pods}
        if self.scale_at is not None and self.clock.now >= self.scale_at:
            state["recommendation-added"] = 0
        return state

    def new_product_id(self):
        product_id = f"PROBE{self._next:05d}"
        self._next += 1
        return product_id

    def insert_probe_product(self, product_id=None):
        if self.insert_broken:
            raise RuntimeError("psql failed")
        product_id = product_id or self.new_product_id()
        self.catalog.append(product_id)
        return product_id

    def delete_product(self, product_id):
        if product_id in self.catalog:
            self.catalog.remove(product_id)

    def measure_upstream(self):
        if self.exec_broken:
            return None
        return {"addr": "product-catalog:8080", "s": self.latency or 0.02, "code": self.upstream_code}

    def release_client(self):
        self.client_released += 1

    def probe_recommendations(self, count, interval_s, deadline_s, exclude_ids=None, pod=None):
        if self.exec_broken:
            return [{"ok": False, "ms": 0.0, "code": "EXEC_FAILED", "error": "exec failed"}]
        exclude = set(exclude_ids or ["OLJCESPC7Z"])
        answers = []
        for _ in range(count):
            if pod:
                self.pod_probes.append(pod)
                target = pod
            else:
                target = self.pods[self._next_pod % len(self.pods)]
                self._next_pod += 1
            answers.append(self._answer(target, exclude, deadline_s))
        return answers

    def probe_frontend(self, count, interval_s, deadline_s, exclude_id):
        if self.exec_broken:
            return [{"ok": False, "ms": 0.0, "code": "EXEC_FAILED", "error": "exec failed"}]
        answers = []
        for _ in range(count):
            target = self.pods[self._next_pod % len(self.pods)]
            self._next_pod += 1
            answer = self._answer(target, {exclude_id}, deadline_s, from_frontend=True)
            if not answer["ok"]:
                answer = {"ok": False, "ms": answer["ms"], "code": "HTTP_500", "error": "Internal Server Error"}
            else:
                answer["ids"] = answer["ids"][:4]
            answers.append(answer)
        return answers

    # The fake recommendation service

    def _refresh(self, pod):
        self.caches[pod] = (list(self.catalog), self.clock.now)

    def _answer(self, pod, exclude, deadline_s, from_frontend=False):
        if self.poller_period_s is not None and self.clock.now - self._polled_at >= self.poller_period_s:
            self._polled_at = self.clock.now
            self._refresh(pod)
        if not self.latency or self.copy_upstream:
            if not self.healthy_ok:
                return {"ok": False, "ms": 20.0, "code": "UNKNOWN", "error": "broken"}
            if self.refreshes and not (self.stuck_after_fallback and self.fell_back):
                self._refresh(pod)
            ids, ms = self.caches[pod][0], 15.0
        elif self.degraded == "error":
            return {"ok": False, "ms": 1000.0, "code": "UNKNOWN", "error": "StatusCode.DEADLINE_EXCEEDED"}
        elif self.degraded == "wait":
            ids, ms = self.catalog, self.latency * 1000 + 5
        elif self.degraded == "cache":
            if self.background_timeout_s and self.latency < self.background_timeout_s:
                self._refresh(pod)
            cache, cached_at = self.caches[pod]
            limited = self.max_age_s is not None and not (self.fingerprint and from_frontend)
            if limited and self.clock.now - cached_at > self.max_age_s:
                self._late += 1
                if self.bypass_after_expiry:
                    self._refresh(pod)
                    cache = self.caches[pod][0]
                elif self.leak_every and self._late % self.leak_every == 0:
                    pass
                elif self.after_expiry == "empty":
                    return {"ok": True, "ms": 1000.0, "ids": []}
                else:
                    return {"ok": False, "ms": 1000.0, "code": "UNKNOWN", "error": "StatusCode.DEADLINE_EXCEEDED"}
            self.fell_back = True
            self._served += 1
            slow = self.tail_every and self._served % self.tail_every == 0
            ids, ms = cache, self.degraded_ms + (1500.0 if slow else 0.0)
        elif self.degraded == "hardcoded":
            ids, ms = CATALOG, 3.0
        elif self.degraded == "hardcoded_unfiltered":
            return {"ok": True, "ms": 3.0, "ids": CATALOG[:5]}
        elif self.degraded == "empty":
            return {"ok": True, "ms": 3.0, "ids": []}
        if ms > deadline_s * 1000:
            return {"ok": False, "ms": deadline_s * 1000, "code": "DEADLINE_EXCEEDED", "error": "Deadline Exceeded"}
        candidates = [i for i in ids if i not in exclude]
        return {"ok": True, "ms": ms, "ids": self._random.sample(candidates, min(5, len(candidates)))}


@pytest.fixture
def evaluate(monkeypatch):
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: {"success": True})
    monkeypatch.setattr(MitigationOracle, "capture_baseline", lambda self: None)

    def run(shop, after_baseline=None, capture=True):
        # The oracle and the fake service share one clock, so waiting in the oracle ages the cache.
        monkeypatch.setattr(oracle_module, "time", shop.clock)
        oracle = MissingStaleCacheFallbackMitigationOracle(problem=shop)
        if capture:
            oracle.capture_baseline()
        if after_baseline:
            after_baseline(shop)
        return oracle.evaluate()

    return run


def _assert_fails(result, reason, failure_class):
    assert result["success"] is False, result
    assert result["reason"] == reason, result
    assert result["failure_class"] == failure_class


def test_cache_fallback_passes(evaluate):
    shop = FakeShop()
    assert evaluate(shop)["success"] is True
    assert shop.client_released >= 1


@pytest.mark.parametrize("delay_s,ms", [(3.0, 2100.0), (3.0, 2700.0), (5.0, 4000.0), (5.0, 4700.0)])
def test_fallback_after_a_timeout_shorter_than_the_delay_passes(evaluate, delay_s, ms):
    assert evaluate(FakeShop(delay_s=delay_s, degraded_ms=ms))["success"] is True


@pytest.mark.parametrize("delay_s,ms", [(3.0, 2800.0), (5.0, 4800.0)])
def test_fallback_after_a_timeout_close_to_the_delay_is_too_slow(evaluate, delay_s, ms):
    result = evaluate(FakeShop(delay_s=delay_s, degraded_ms=ms))
    _assert_fails(result, "recommendations_too_slow_under_degradation", "agent_error")
    assert result["detail"]["limit_s"] == delay_s - 0.25


@pytest.mark.parametrize("tail_every,passes", [(10, True), (6, False)])
def test_a_few_slow_calls_are_tolerated(evaluate, tail_every, passes):
    # 20 probes at 1.5 s: every 10th slow (3.0 s) leaves 18 fast, every 6th leaves 17.
    shop = FakeShop(degraded_ms=1500.0)
    shop.tail_every = tail_every
    result = evaluate(shop)
    assert result["success"] is passes
    if not passes:
        assert result["reason"] == "recommendations_too_slow_under_degradation"


def test_buggy_code_fails_under_degradation(evaluate):
    _assert_fails(evaluate(FakeShop(degraded="error")), "recommendations_failing_under_degradation", "agent_error")


def test_waiting_out_the_delay_is_too_slow(evaluate):
    _assert_fails(evaluate(FakeShop(degraded="wait")), "recommendations_too_slow_under_degradation", "agent_error")


@pytest.mark.parametrize(
    "degraded,reason",
    [
        ("hardcoded", "recommendations_invalid"),
        ("hardcoded_unfiltered", "recommendations_invalid"),
        ("empty", "recommendations_empty"),
    ],
)
def test_answers_not_from_the_cached_catalog_fail(evaluate, degraded, reason):
    _assert_fails(evaluate(FakeShop(degraded=degraded)), reason, "agent_error")


def test_service_that_stopped_reading_the_catalog_fails(evaluate):
    shop = FakeShop()
    shop.refreshes = False
    _assert_fails(evaluate(shop), "catalog_changes_not_picked_up", "agent_error")


def test_service_that_never_refreshes_after_falling_back_fails(evaluate):
    shop = FakeShop()
    shop.stuck_after_fallback = True
    _assert_fails(evaluate(shop), "catalog_changes_not_picked_up", "agent_error")


def test_broken_service_fails_when_healthy(evaluate):
    shop = FakeShop()
    shop.healthy_ok = False
    _assert_fails(evaluate(shop), "recommendations_failing_when_healthy", "agent_error")


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "ms": 10.0, "code": "HTTP_503"},
        {"ok": False, "ms": 10.0, "code": "UNAVAILABLE"},
        {"ok": True, "ms": 10.0, "ids": []},
        {"ok": True, "ms": 10.0, "ids": CATALOG[:4]},
        {"ok": True, "ms": 10.0, "ids": ["PROBE00000", "NOT_IN_CATALOG"]},
    ],
)
def test_broken_or_invalid_frontend_fails_while_recommendation_is_healthy(evaluate, answer):
    shop = FakeShop()
    shop.probe_frontend = Mock(return_value=[answer])
    result = evaluate(shop)
    _assert_fails(result, "frontend_unhealthy", "agent_error")
    assert "with product-catalog healthy" in result["detail"]["message"]


def test_frontend_must_recover_after_cache_expiry(evaluate):
    shop = FakeShop()
    probe = shop.probe_frontend

    def unavailable_after_recovery(*args, **kwargs):
        if not shop.latency and shop.fell_back:
            return [{"ok": False, "ms": 10.0, "code": "HTTP_503"}]
        return probe(*args, **kwargs)

    shop.probe_frontend = unavailable_after_recovery
    result = evaluate(shop)
    _assert_fails(result, "frontend_unhealthy", "agent_error")
    assert "after product-catalog recovered" in result["detail"]["message"]


@pytest.mark.parametrize("code", ["EXEC_FAILED", "NO_OUTPUT"])
def test_unavailable_frontend_probe_is_an_environment_error(evaluate, code):
    shop = FakeShop()
    shop.probe_frontend = Mock(return_value=[{"ok": False, "ms": 0.0, "code": code}])
    _assert_fails(evaluate(shop), "probe_unavailable", "environment_error")


def test_frontend_can_retry_a_transient_failure(evaluate):
    shop = FakeShop()
    probe = shop.probe_frontend
    failed = False

    def transient(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            return [{"ok": False, "ms": 10.0, "code": "HTTP_503"}]
        return probe(*args, **kwargs)

    shop.probe_frontend = transient
    assert evaluate(shop)["success"] is True


def test_frontend_can_rank_other_valid_products_ahead_of_the_new_product(evaluate):
    shop = FakeShop()
    probe = shop.probe_frontend

    def ranked_frontend(count, interval_s, deadline_s, exclude_id):
        answers = probe(count, interval_s, deadline_s, exclude_id)
        for answer in answers:
            if answer["ok"] and answer["ids"]:
                answer["ids"] = sorted(set(shop.caches[shop.pods[0]][0]) - {exclude_id})[:4]
        return answers

    shop.probe_frontend = ranked_frontend
    assert evaluate(shop)["success"] is True


def test_frontend_cannot_keep_serving_old_products_after_cache_expiry(evaluate):
    shop = FakeShop()
    shop.probe_frontend = Mock(return_value=[{"ok": True, "ms": 10.0, "ids": CATALOG[1:5]}])
    _assert_fails(evaluate(shop), "stale_data_served_past_limit", "agent_error")


def test_every_replica_gets_the_new_catalog(evaluate):
    shop = FakeShop(replicas=2)
    assert evaluate(shop)["success"] is True
    assert set(shop.pod_probes) == set(shop.pods)


def test_undelayed_upstream_copy_fails(evaluate):
    # Reading the catalog around the impaired product-catalog is not a fix.
    shop = FakeShop(degraded="error")
    shop.copy_upstream = True
    _assert_fails(evaluate(shop), "upstream_bypassed", "agent_error")


@pytest.mark.parametrize(
    "name,field,value",
    [
        ("recommendation", "selector", {"app": "recommendation-v2"}),
        ("product-catalog", "selector", {"app": "product-catalog-v2"}),
        ("recommendation", "session_affinity", "ClientIP"),
        ("recommendation-dns", "host_aliases", [{"ip": "10.96.34.212", "hostnames": ["product-catalog"]}]),
    ],
)
def test_routing_changed_since_injection_fails(evaluate, name, field, value):
    def change(shop):
        shop.routing[name] = {**shop.routing[name], field: value}

    _assert_fails(evaluate(FakeShop(), after_baseline=change), "upstream_bypassed", "agent_error")


def test_reading_around_the_delay_once_the_cache_expires_fails(evaluate):
    shop = FakeShop()
    shop.bypass_after_expiry = True
    _assert_fails(evaluate(shop), "upstream_bypassed", "agent_error")


@pytest.mark.parametrize("period_s", [5.0, 30.0, 60.0])
def test_background_poller_reading_around_the_delay_fails(evaluate, period_s):
    shop = FakeShop()
    shop.poller_period_s = period_s
    _assert_fails(evaluate(shop), "upstream_bypassed", "agent_error")


def test_cache_leaking_past_the_age_limit_fails(evaluate):
    shop = FakeShop()
    shop.leak_every = 10
    _assert_fails(evaluate(shop), "stale_data_served_past_limit", "agent_error")


@pytest.mark.parametrize("max_age_s", [None, 150.0, 600.0])
def test_cache_served_past_the_age_limit_fails(evaluate, max_age_s):
    _assert_fails(evaluate(FakeShop(max_age_s=max_age_s)), "stale_data_served_past_limit", "agent_error")


@pytest.mark.parametrize("max_age_s,after_expiry", [(120.0, "error"), (60.0, "error"), (120.0, "empty")])
def test_cache_with_an_age_limit_passes(evaluate, max_age_s, after_expiry):
    shop = FakeShop(max_age_s=max_age_s)
    shop.after_expiry = after_expiry
    assert evaluate(shop)["success"] is True


def test_age_limit_only_for_the_oracles_own_client_fails(evaluate):
    shop = FakeShop()
    shop.fingerprint = True
    _assert_fails(evaluate(shop), "stale_data_served_past_limit", "agent_error")


def test_cache_refreshed_in_the_background_through_product_catalog_passes(evaluate):
    # A 10s background fetch gets through the 3s delay. While the oracle holds product-catalog it can't, so the
    # cache still has to expire after 2 minutes.
    shop = FakeShop(max_age_s=120.0)
    shop.background_timeout_s = 10.0
    assert evaluate(shop)["success"] is True


def test_background_refresh_without_an_age_limit_fails(evaluate):
    shop = FakeShop(max_age_s=None)
    shop.background_timeout_s = 10.0
    _assert_fails(evaluate(shop), "stale_data_served_past_limit", "agent_error")


def test_restart_while_impaired_fails(evaluate):
    shop = FakeShop()
    shop.restart_at = 30.0
    _assert_fails(evaluate(shop), "recommendation_restarted_under_degradation", "agent_error")


def test_pod_added_while_impaired_is_ambiguous(evaluate):
    shop = FakeShop()
    shop.scale_at = 30.0
    _assert_fails(evaluate(shop), "recommendation_scaled_under_degradation", "ambiguous")


def test_deleted_original_products_fail(evaluate):
    result = evaluate(FakeShop(), after_baseline=lambda shop: shop.catalog.clear())
    _assert_fails(result, "catalog_data_lost", "agent_error")
    assert result["detail"]["ids"] == CATALOG


def test_one_changed_original_product_fails(evaluate):
    def change(shop):
        rows = shop.catalog_rows
        shop.catalog_rows = lambda: [r.replace("|19", "|0") if r.startswith("66VCHSJNUP") else r for r in rows()]

    result = evaluate(FakeShop(), after_baseline=change)
    _assert_fails(result, "catalog_data_lost", "agent_error")
    assert result["detail"]["ids"] == ["66VCHSJNUP"]


def test_unreadable_catalog_is_ambiguous(evaluate):
    def break_reads(shop):
        shop.catalog_rows = Mock(side_effect=RuntimeError("psql: connection refused"))

    _assert_fails(evaluate(FakeShop(), after_baseline=break_reads), "catalog_unavailable", "ambiguous")


def test_baseline_that_cannot_be_recorded_fails_loudly(monkeypatch):
    monkeypatch.setattr(MitigationOracle, "capture_baseline", lambda self: None)
    shop = FakeShop()
    shop.catalog_rows = Mock(side_effect=RuntimeError("psql failed"))
    monkeypatch.setattr(oracle_module, "time", shop.clock)
    oracle = MissingStaleCacheFallbackMitigationOracle(problem=shop)
    with pytest.raises(RuntimeError, match="could not record"):
        oracle.capture_baseline()
    assert shop.catalog_rows.call_count == 3


def test_evaluation_without_a_baseline_is_environmental(evaluate):
    _assert_fails(evaluate(FakeShop(), capture=False), "baseline_missing", "environment_error")


def test_failing_upstream_is_ambiguous(evaluate):
    shop = FakeShop()
    shop.upstream_code = "UNAVAILABLE"
    _assert_fails(evaluate(shop), "upstream_unhealthy", "ambiguous")


def test_delay_that_cannot_be_lifted_is_environmental(evaluate):
    shop = FakeShop()
    shop.can_lift = False
    _assert_fails(evaluate(shop), "impairment_unavailable", "environment_error")


def test_delay_that_cannot_be_applied_is_environmental(evaluate):
    shop = FakeShop()
    shop.can_apply = False
    _assert_fails(evaluate(shop), "impairment_unavailable", "environment_error")


def test_probe_that_cannot_run_is_environmental(evaluate):
    shop = FakeShop()
    shop.exec_broken = True
    _assert_fails(evaluate(shop), "probe_unavailable", "environment_error")


def test_catalog_that_cannot_take_a_probe_product_is_environmental(evaluate):
    shop = FakeShop()
    shop.insert_broken = True
    _assert_fails(evaluate(shop), "catalog_probe_setup_failed", "environment_error")


@pytest.mark.parametrize("degraded", ["cache", "error", "hardcoded"])
def test_probe_products_are_deleted(evaluate, degraded):
    shop = FakeShop(degraded=degraded)
    evaluate(shop)
    assert sorted(shop.catalog) == CATALOG


@pytest.mark.parametrize("degraded", ["cache", "error"])
def test_running_incident_is_restored(evaluate, degraded):
    shop = FakeShop(degraded=degraded)
    evaluate(shop)
    assert shop.restored[-1] is True
    assert shop.schedule and shop.latency == 3.0


def test_delay_stays_off_when_no_incident_was_running(evaluate):
    shop = FakeShop()
    shop.schedule = False
    evaluate(shop)
    assert not shop.schedule and not shop.latency


def _late(oracle, results):
    return oracle._late_failure(results, "P3", {})


def test_late_answers_may_fail_or_be_empty():
    oracle = MissingStaleCacheFallbackMitigationOracle(problem=FakeShop())
    oracle.problem.rollout_state = lambda: {}
    answers = [{"ok": False, "ms": 1000.0, "code": "HTTP_500"}, {"ok": True, "ms": 1000.0, "ids": []}]
    assert _late(oracle, answers) is None


def test_one_late_answer_with_cached_data_is_stale():
    oracle = MissingStaleCacheFallbackMitigationOracle(problem=FakeShop())
    oracle.problem.rollout_state = lambda: {}
    answers = [{"ok": False, "ms": 1000.0, "code": "HTTP_500"}] * 29 + [{"ok": True, "ms": 5.0, "ids": ["1YMWWN1N4O"]}]
    _assert_fails(_late(oracle, answers), "stale_data_served_past_limit", "agent_error")


def test_late_answer_with_the_held_back_product_is_a_bypass():
    oracle = MissingStaleCacheFallbackMitigationOracle(problem=FakeShop())
    oracle.problem.rollout_state = lambda: {}
    _assert_fails(_late(oracle, [{"ok": True, "ms": 5.0, "ids": ["P1", "P3"]}]), "upstream_bypassed", "agent_error")
