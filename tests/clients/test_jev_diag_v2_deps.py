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


def test_containers_of_one_pod_that_listen_on_the_same_port_are_flagged():
    from clients.jev_diag.v2.checks import command_ports, pod_port_collisions

    assert command_ports({"command": ["sh", "-c", "exec nc -lk -p 5000"]}) == {5000}
    assert command_ports({"args": ["--port=8080", "--listen-addr", "0.0.0.0:9090"]}) == {8080, 9090}
    assert command_ports({"args": ["--timeout=30s", "--upstream", "10.0.0.1:80", "curl localhost:5000"]}) == set()
    spec = {
        "initContainers": [{"name": "init", "command": ["nc", "-l", "-p", "5000"]}],
        "containers": [
            {"name": "app", "ports": [{"containerPort": 5000}], "args": ["--port", "5000"]},
            {"name": "sidecar", "command": ["sh", "-c", "exec nc -lk -p 5000"]},
        ],
    }
    assert pod_port_collisions(spec) == [
        "containers app and sidecar of one pod both listen on port 5000 (app declares it, sidecar binds it in its "
        "command); a pod's containers share one network namespace, so only one of them can bind it"
    ]
    assert pod_port_collisions({"containers": [{"name": "app", "ports": [{"containerPort": 80}]}]}) == []


def test_a_host_port_another_workload_binds_is_flagged():
    from clients.jev_diag.v2.checks import host_port_clashes

    comps = _app()
    comps["deployment/frontend"]["containers"][0]["host_ports"] = [9100]
    daemonset = {
        "metadata": {"namespace": "observe", "name": "node-exporter"},
        "spec": {"template": {"spec": {"containers": [{"name": "x", "ports": [{"hostPort": 9100}]}]}}},
    }
    host_port_clashes(comps, [daemonset])
    assert comps["deployment/frontend"]["signals"] == [
        "container frontend requests hostPort 9100, which DaemonSet observe/node-exporter also binds; a node offers "
        "each host port once, so their pods cannot run on the same node"
    ]
    assert not comps["deployment/orders"]["signals"]


def test_the_report_states_the_settings_behind_the_fault_type():
    from clients.jev_diag.v2.classifier import ChoiceResult, Diagnosis
    from clients.jev_diag.v2.report import build_report

    probe = "container frontend liveness probe: httpGet /health:8080 (initialDelaySeconds=0, periodSeconds=1)"
    comp = {**_server("frontend"), "signals": ["container frontend restarted 9 times"]}
    d = Diagnosis(
        component="deployment/frontend",
        component_result=ChoiceResult(choice="deployment/frontend", confidence=1.0, probabilities={}),
        fault_visible=None,
        category_result=ChoiceResult(choice="health_probe_config", confidence=1.0, probabilities={}),
        evidence_result=ChoiceResult(choice="E1", confidence=1.0, probabilities={}),
        evidence=[{"id": "E1", "text": "container frontend restarted 9 times"}, {"id": "E2", "text": probe}],
        model="m",
        input_tokens=None,
        output_tokens=None,
        investigation={"steps": []},
    )
    text = build_report(
        ClusterSnapshot(app={"namespaces": [NS]}, components={"deployment/frontend": comp}, cluster={}), d
    )
    assert f"Details: {probe}." in text


def test_the_change_log_keeps_operator_writes_after_deploy_and_leaves_out_the_rest(monkeypatch):
    from clients.jev_diag.timeutil import parse_time, set_now
    from clients.jev_diag.v2 import outside

    def obj(kind, name, ns=None, *writes):
        return {
            "kind": kind,
            "metadata": {
                "name": name,
                "namespace": ns,
                "creationTimestamp": "2026-10-19T07:00:00Z",
                "managedFields": [{"manager": m, "operation": "Update", "time": t} for m, t in writes],
            },
        }

    items = {
        "ippools.crd.projectcalico.org": [
            obj(
                "IPPool", "default", None, ("calico", "2026-10-19T07:00:00Z"), ("kubectl-patch", "2026-10-19T08:05:00Z")
            )
        ],
        "nodes": [obj("Node", "worker2", None, ("kubelet", "2026-10-19T08:06:00Z"))],
        "clusterroles.rbac.authorization.k8s.io": [
            obj("ClusterRole", "mcp-server", None, ("kubectl", "2026-10-19T08:04:00Z"))
        ],
        "configmaps": [
            obj("ConfigMap", "app-config", NS, ("kubectl-edit", "2026-10-19T08:05:00Z")),
            obj("ConfigMap", "dns", "kube-system", ("kubectl-client-side-apply", "2026-10-19T06:00:00Z")),
        ],
        "networkchaos.chaos-mesh.org": [obj("NetworkChaos", "x", NS, ("OpenAPI-Generator", "2026-10-19T08:05:00Z"))],
    }
    monkeypatch.setattr(outside, "run_kubectl", lambda args: " ".join(items))
    monkeypatch.setattr(outside, "kubectl_items", lambda resource, ns, errors: items[resource])
    set_now(parse_time("2026-10-19T08:10:00Z"))
    try:
        changes: list[dict] = []
        found = outside.change_radar([NS], _app(), [], changes, parse_time("2026-10-19T08:00:00Z"))
    finally:
        set_now(None)
    assert [(f["kind"], f["name"]) for f in found] == [("IPPool", "default")]
    assert found[0]["signals"] == ["written by kubectl-patch after the application was deployed"]
    assert [c["object"] for c in changes] == ["ippool/default"]


def test_a_policy_cause_is_reported_with_its_scope():
    from clients.jev_diag.v2.classifier import ChoiceResult, Diagnosis
    from clients.jev_diag.v2.report import scope

    def diagnosis(fault_object: dict) -> Diagnosis:
        return Diagnosis(
            component="deployment/frontend",
            component_result=ChoiceResult(choice="deployment/frontend", confidence=1.0, probabilities={}),
            fault_visible=None,
            category_result=None,
            evidence_result=None,
            evidence=[],
            model="m",
            input_tokens=None,
            output_tokens=None,
            fault_object=fault_object,
        )

    comps = _app()
    comps["deployment/orders"]["containers"][0]["limits"] = {"memory": "1Gi"}
    cluster = {
        "admission_webhooks": [
            {"configuration": "policy", "rules": ["CREATE pods"], "namespace_selector": {"matchLabels": {"team": "a"}}}
        ],
        "namespace_constraints": [
            {
                "kind": "LimitRange",
                "name": "mem",
                "namespace": NS,
                "limits": [{"type": "Container", "max": {"memory": "512Mi"}}],
            }
        ],
    }
    snap = ClusterSnapshot(app={"namespaces": [NS]}, components=comps, cluster=cluster)
    webhook = scope(snap, diagnosis({"kind": "MutatingWebhookConfiguration", "name": "policy"}))
    assert webhook == 'it intercepts CREATE pods in namespaces matching {"matchLabels": {"team": "a"}}'
    limit = scope(snap, diagnosis({"kind": "LimitRange", "name": "mem", "namespace": NS}))
    assert limit == (
        f"it applies to every pod created in namespace `{NS}`; `orders` also violates it, so their pods are rejected "
        "when next created"
    )
    assert scope(snap, diagnosis({"kind": "ConfigMap", "name": "x", "namespace": NS})) is None
