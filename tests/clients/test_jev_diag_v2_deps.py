"""jev_diag v2 deps.py: the dependency graph and what follows from it."""

from __future__ import annotations

from clients.jev_diag.v2 import deps
from clients.jev_diag.v2.collector import ClusterSnapshot
from clients.jev_diag.v2.tree import explaining_dependency

NS = "shop"


def _server(name: str, *, service: bool = True, signals: list[str] | None = None, **extra) -> dict:
    return {
        "kind": "Deployment",
        "name": name,
        "namespace": NS,
        "containers": [{"name": name, "image": f"img/{name}", "ports": [8080]}],
        "services": [{"name": name}] if service else [],
        "pods": [{"name": f"{name}-1", "ready": "1/1"}],
        "signals": list(signals or []),
        **extra,
    }


def _app(**overrides: dict) -> dict[str, dict]:
    comps = {f"deployment/{n}": _server(n) for n in ("frontend", "orders", "order-history-service", "user-service")}
    comps["deployment/mongodb-orders"] = _server("mongodb-orders")
    comps["deployment/order-history-redis"] = _server("order-history-redis")
    comps["deployment/jaeger"] = _server("jaeger")
    comps.update(overrides)
    return comps


def test_a_data_store_belongs_to_the_one_service_it_is_named_after():
    edges = deps.naming_edges(_app())
    assert edges == {
        "deployment/orders": {"deployment/mongodb-orders"},
        "deployment/order-history-service": {"deployment/order-history-redis"},
    }


def test_traced_calls_and_addresses_join_the_graph_with_their_sources():
    comps = _app(**{"deployment/frontend": _server("frontend", _calls=["deployment/user-service"])})
    deps.build_graph(comps, [{"parent": "frontend", "child": "orders", "calls": 9}, {"parent": "x", "child": "orders"}])
    assert comps["deployment/frontend"]["_deps"] == {
        "deployment/orders": ["traces"],
        "deployment/user-service": ["address"],
    }
    assert comps["deployment/orders"]["_deps"] == {"deployment/mongodb-orders": ["naming"]}


def test_a_server_no_service_selects_is_flagged_when_the_others_have_one():
    comps = _app(**{"deployment/mongodb-orders": _server("mongodb-orders", service=False)})
    deps.build_graph(comps, [])
    deps.flag_unrouted_servers(comps)
    assert comps["deployment/mongodb-orders"]["signals"][0].startswith("no Service selects this workload")
    assert "(clients: deployment/orders)" in comps["deployment/mongodb-orders"]["signals"][0]
    assert not any(c["signals"] for cid, c in comps.items() if cid != "deployment/mongodb-orders")


def test_unnamed_connection_errors_point_at_the_dependency_broken_on_its_own():
    failing = {"classes": ["connectivity or timeout"], "count": 7, "targets": [], "line": "no reachable servers"}
    comps = _app(
        **{
            "deployment/orders": _server("orders", log_findings=[failing]),
            "deployment/mongodb-orders": _server("mongodb-orders", service=False),
        }
    )
    snap = ClusterSnapshot(app={"namespaces": [NS]}, components=comps, cluster={})
    deps.apply(snap)
    assert comps["deployment/orders"]["errors_point_to"] == ["deployment/mongodb-orders"]
    assert comps["deployment/mongodb-orders"]["referenced_by_errors_from"] == {
        "deployment/orders": {"connectivity or timeout": 7}
    }
    assert explaining_dependency(snap, "deployment/orders", {}) == "deployment/mongodb-orders"
    assert explaining_dependency(snap, "deployment/orders", {"deployment/mongodb-orders": {}}) is None


def test_healthy_dependencies_take_no_blame_for_unnamed_errors():
    failing = {"classes": ["connectivity or timeout"], "count": 7, "targets": [], "line": "timeout"}
    comps = _app(**{"deployment/orders": _server("orders", log_findings=[failing])})
    snap = ClusterSnapshot(app={"namespaces": [NS]}, components=comps, cluster={})
    deps.apply(snap)
    assert "errors_point_to" not in comps["deployment/orders"]
    assert explaining_dependency(snap, "deployment/orders", {}) is None


def test_an_address_no_service_provides_names_the_workload_whose_service_is_missing():
    comps = _app(
        **{
            "deployment/frontend": _server("frontend", _env_addresses=["orders", "user-service", "mongodb-orders"]),
            "deployment/orders": _server(
                "orders", _env_addresses=["mongodb-orders.shop.svc.cluster.local", "smtp.example.com"]
            ),
            "deployment/mongodb-orders": _server("mongodb-orders", service=False),
        }
    )
    deps.build_graph(comps, [])
    deps.flag_dangling_addresses(comps, [])
    signal = comps["deployment/orders"]["signals"][0]
    assert signal.startswith(
        "env value addresses host mongodb-orders.shop.svc.cluster.local, but no Service mongodb-orders"
    )
    assert signal.endswith("workload mongodb-orders exists but no Service exposes it")
    assert comps["deployment/orders"]["_deps"]["deployment/mongodb-orders"] == ["naming", "address"]
    assert len(comps["deployment/frontend"]["signals"]) == 1  # orders and user-service resolve


def test_addresses_are_not_judged_in_an_application_that_does_not_address_by_service():
    comps = _app(**{"deployment/frontend": _server("frontend", _env_addresses=["cart"])})
    deps.flag_dangling_addresses(comps, [])
    assert not comps["deployment/frontend"]["signals"]


def test_env_addresses_take_urls_and_host_ports_but_never_bare_words():
    from clients.jev_diag.v2.checks import env_addresses

    assert env_addresses("http://cart:7070/api, redis:6379 info $(HOST):1") == {"cart", "redis"}


def test_reachability_says_where_the_path_from_a_client_breaks(monkeypatch):
    from clients.jev_diag.v2 import probes

    comps = _app(**{"deployment/mongodb-orders": _server("mongodb-orders", service=False)})
    comps["deployment/orders"]["pods"] = [{"name": "orders-1", "ready": "1/1", "phase": "Running"}]
    snap = ClusterSnapshot(app={"namespaces": [NS]}, components=comps, cluster={})
    calls = []

    def fake(ns, pod, container, command):
        calls.append(command[-2:])
        return 0, "resolve none\nconnect fail nc: bad address 'mongodb-orders'"

    monkeypatch.setattr(probes, "run_in_pod", fake)
    result = probes.reachability(snap, "deployment/orders", "deployment/mongodb-orders")
    assert calls == [["mongodb-orders", "8080"]]
    assert result["meaning"] == "the name does not resolve: no Service or DNS record answers it"
    assert probes.describe_reachability(result).startswith(
        "from deployment/orders, mongodb-orders:8080 (deployment/mongodb-orders)"
    )
    assert probes.reach_meaning("10.0.0.1", "nc: connect to x port 80 (tcp) failed: Connection refused").endswith(
        "nothing listens behind that port"
    )
    assert probes.reach_meaning("10.0.0.1", "ok").endswith("the failure lies above the connection")
    assert probes.reach_meaning("unknown", "unknown") is None

    monkeypatch.setattr(probes, "run_in_pod", lambda *a: (127, "sh: not found"))
    assert probes.reachability(snap, "deployment/orders", "deployment/mongodb-orders") is None
