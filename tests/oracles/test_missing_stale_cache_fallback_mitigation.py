from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

import sregym.conductor.oracles.missing_stale_cache_fallback_mitigation as oracle_module
from sregym.conductor.oracles.missing_stale_cache_fallback_mitigation import MissingStaleCacheFallbackMitigationOracle
from sregym.conductor.oracles.mitigation import MitigationOracle
from sregym.conductor.problems.missing_stale_cache_fallback_astronomy_shop import MissingStaleCacheFallbackAstronomyShop

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


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeShop:
    """A recommendation service and its upstream, with the agent's fix chosen per test"""

    UPSTREAM_ADDR = "product-catalog:8080"

    def __init__(self, delay_s=3.0, degraded="cache", degraded_ms=1100.0):
        self.delay = f"{delay_s:g}s"
        self.delay_seconds = delay_s
        self.delay_active_min_s = 0.8 * delay_s
        self.catalog = list(CATALOG)
        self.cache = list(CATALOG)
        self.schedule = True
        self.delayed = True
        # What the fixed service does when product-catalog is delayed: cache, error, wait, hardcoded, empty.
        self.degraded = degraded
        self.degraded_ms = degraded_ms
        self.healthy_ok = True
        # Every n-th answer from the cache is 1.5 s slower (background load). 0 means never.
        self.tail_every = 0
        self._served = 0
        self.refreshes = True
        self.stuck_after_fallback = False
        self.fell_back = False
        self.bypassed = False
        self.can_lift = True
        self.can_apply = True
        self.upstream_code = "OK"
        self.exec_broken = False
        self.insert_broken = False
        self.restored = []
        self._next = 0

    def incident_running(self):
        return self.schedule

    def remove_delay(self):
        self.schedule = False
        if not self.can_lift:
            raise RuntimeError("product-catalog did not answer quickly after removing the delay")
        self.delayed = False
        if self.upstream_code != "OK":
            raise RuntimeError("product-catalog did not answer quickly after removing the delay")

    def apply_delay(self, keep_alive):
        self.schedule = keep_alive
        self.delayed = self.can_apply
        self.restored.append(keep_alive)

    def wait_for_delay(self, active):
        return self.delayed is active

    def wait_until_answering(self):
        if not self.healthy_ok:
            raise RuntimeError("Recommendation is not answering")

    def catalog_ids(self):
        return sorted(self.catalog)

    def insert_probe_product(self):
        if self.insert_broken:
            raise RuntimeError("psql failed")
        product_id = f"PROBE{self._next:05d}"
        self._next += 1
        self.catalog.append(product_id)
        return product_id

    def delete_product(self, product_id):
        self.catalog.remove(product_id)

    def measure_upstream(self, address=""):
        if self.exec_broken:
            return None
        slow = self.delayed and not (address == "" and self.bypassed)
        return {
            "addr": address or "catalog-copy:8080",
            "s": self.delay_seconds if slow else 0.02,
            "code": self.upstream_code,
        }

    def probe_recommendations(self, count, interval_s, deadline_s, exclude_ids=None):
        if self.exec_broken:
            return [{"ok": False, "ms": 0.0, "code": "EXEC_FAILED", "error": "exec failed"}]
        exclude = set(exclude_ids or ["OLJCESPC7Z"])
        return [self._answer(exclude, deadline_s) for _ in range(count)]

    def _answer(self, exclude, deadline_s):
        if not self.delayed or self.bypassed:
            if not self.healthy_ok:
                return {"ok": False, "ms": 20.0, "code": "UNKNOWN", "error": "broken"}
            if self.refreshes and not (self.stuck_after_fallback and self.fell_back):
                self.cache = list(self.catalog)
            ids, ms = self.cache, 15.0
        elif self.degraded == "error":
            return {"ok": False, "ms": 1000.0, "code": "UNKNOWN", "error": "Deadline Exceeded"}
        elif self.degraded == "wait":
            ids, ms = self.catalog, self.delay_seconds * 1000 + 5
        elif self.degraded == "cache":
            self.fell_back = True
            self._served += 1
            slow = self.tail_every and self._served % self.tail_every == 0
            ids, ms = self.cache, self.degraded_ms + (1500.0 if slow else 0.0)
        elif self.degraded == "hardcoded":
            ids, ms = CATALOG[:5], 3.0
        elif self.degraded == "hardcoded_unfiltered":
            return {"ok": True, "ms": 3.0, "ids": CATALOG[:5]}
        elif self.degraded == "empty":
            return {"ok": True, "ms": 3.0, "ids": []}
        if ms > deadline_s * 1000:
            return {"ok": False, "ms": deadline_s * 1000, "code": "DEADLINE_EXCEEDED", "error": "Deadline Exceeded"}
        return {"ok": True, "ms": ms, "ids": [i for i in ids if i not in exclude][:5]}


@pytest.fixture
def evaluate(monkeypatch):
    monkeypatch.setattr(MitigationOracle, "evaluate", lambda self: {"success": True})
    monkeypatch.setattr(oracle_module, "time", Clock())

    def run(shop):
        return MissingStaleCacheFallbackMitigationOracle(problem=shop).evaluate()

    return run


def _assert_fails(result, reason, failure_class):
    assert result["success"] is False
    assert result["reason"] == reason
    assert result["failure_class"] == failure_class


def test_cache_fallback_passes(evaluate):
    assert evaluate(FakeShop())["success"] is True


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
        ("hardcoded", "recommendations_empty"),
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


def test_undelayed_upstream_copy_fails(evaluate):
    shop = FakeShop(degraded="error")
    shop.bypassed = True
    _assert_fails(evaluate(shop), "upstream_bypassed", "agent_error")


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
    assert shop.schedule and shop.delayed


def test_delay_stays_off_when_no_incident_was_running(evaluate):
    shop = FakeShop()
    shop.schedule = False
    evaluate(shop)
    assert not shop.schedule and not shop.delayed


# The problem's own helpers, without a cluster.


@pytest.fixture
def problem():
    problem = MissingStaleCacheFallbackAstronomyShop.__new__(MissingStaleCacheFallbackAstronomyShop)
    problem.namespace = "astronomy-shop"
    problem.delay = "3s"
    problem.delay_seconds = 3.0
    problem.delay_active_min_s = 2.4
    problem.kubectl = SimpleNamespace(exec_command=Mock(return_value=""), exec_command_checked=Mock(return_value=""))
    return problem


@pytest.mark.parametrize("delay", ["0s", "1.5s", "3", "3ms", "-3s"])
def test_delay_too_short_or_malformed_is_rejected(delay):
    with pytest.raises(ValueError):
        MissingStaleCacheFallbackAstronomyShop._parse_delay(delay)


@pytest.mark.parametrize("delay,seconds", [("2s", 2.0), ("3s", 3.0), ("5s", 5.0), ("2.5s", 2.5)])
def test_delay_is_parsed(delay, seconds):
    assert MissingStaleCacheFallbackAstronomyShop._parse_delay(delay) == seconds


@pytest.mark.parametrize("keep_alive,kind", [(True, "Schedule"), (False, "NetworkChaos")])
def test_delay_lives_in_the_app_namespace_and_selects_by_deployment_label(problem, keep_alive, kind):
    problem.apply_delay(keep_alive=keep_alive)
    manifest = yaml.safe_load(problem.kubectl.exec_command_checked.call_args.kwargs["input_data"])
    assert manifest["kind"] == kind
    assert manifest["metadata"]["namespace"] == "astronomy-shop"
    spec = manifest["spec"]["networkChaos"] if keep_alive else manifest["spec"]
    assert spec["selector"]["labelSelectors"] == {"opentelemetry.io/name": "product-catalog"}
    assert spec["target"]["selector"]["labelSelectors"] == {"opentelemetry.io/name": "recommendation"}
    assert spec["delay"]["latency"] == "3s"


def test_chaos_delete_is_bounded_and_uses_full_resource_names(problem):
    problem._delete_chaos()
    delete = problem.kubectl.exec_command.call_args_list[0].args[0]
    assert "schedules.chaos-mesh.org,networkchaos.chaos-mesh.org catalog-latency -n astronomy-shop" in delete
    assert "--timeout=60s" in delete


@pytest.mark.parametrize(
    "measured,expected",
    [
        (None, None),
        ({"addr": "a", "s": 3.01, "code": "OK"}, True),
        ({"addr": "a", "s": 9.0, "code": "DEADLINE_EXCEEDED"}, True),
        ({"addr": "a", "s": 0.02, "code": "OK"}, False),
        ({"addr": "a", "s": 0.01, "code": "UNAVAILABLE"}, None),
    ],
)
def test_delay_active_is_unknown_unless_measured(problem, measured, expected):
    problem.measure_upstream = Mock(return_value=measured)
    assert problem.delay_active() is expected
    assert problem.measure_upstream.call_args.args[0] == "product-catalog:8080"


def test_wait_for_delay_needs_an_explicit_answer(problem, monkeypatch):
    monkeypatch.setattr("sregym.conductor.problems.missing_stale_cache_fallback_astronomy_shop.time", Clock())
    problem.delay_active = Mock(return_value=None)
    assert problem.wait_for_delay(active=False) is False


@pytest.mark.parametrize(
    "error",
    [
        "failed to connect to all addresses; last error: UNKNOWN: ipv4:127.0.0.1:8080: Connection refused",
        "failed to connect to all addresses; last error: UNKNOWN: ipv6:%5B::1%5D:8080: Failed to connect",
    ],
)
def test_wait_until_answering_waits_for_the_local_port(problem, monkeypatch, error):
    monkeypatch.setattr("sregym.conductor.problems.missing_stale_cache_fallback_astronomy_shop.time", Clock())
    refused = {"ok": False, "ms": 1.0, "code": "UNAVAILABLE", "error": error}
    problem.probe_recommendations = Mock(side_effect=[[refused], [refused], [{"ok": True, "ms": 5.0, "ids": []}]])
    problem.wait_until_answering()
    assert problem.probe_recommendations.call_count == 3
