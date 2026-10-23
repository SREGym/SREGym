"""jev_diag v2 deps.py: the dependency graph and what follows from it."""

from __future__ import annotations

from clients.jev_diag.v2 import deps
from clients.jev_diag.v2.collector import ClusterSnapshot

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


def test_a_server_no_service_selects_is_flagged_when_a_client_needs_it():
    comps = _app(**{"deployment/mongodb-orders": _server("mongodb-orders", service=False)})
    deps.build_graph(comps, [])
    deps.flag_unrouted_servers(comps)
    assert not comps["deployment/mongodb-orders"]["signals"]  # a naming edge to a healthy component: nothing needs it
    comps["deployment/orders"]["log_findings"] = [{"classes": ["connectivity or timeout"], "count": 7}]
    deps.flag_unrouted_servers(comps)
    (hint,) = comps["deployment/mongodb-orders"]["signals"]
    assert "deployment/orders, named for it, fails reaching its dependencies" in hint
    comps["deployment/mongodb-orders"]["signals"] = []
    comps["deployment/orders"]["_calls"] = ["deployment/mongodb-orders"]
    deps.build_graph(comps, [])
    deps.flag_unrouted_servers(comps)
    (signal,) = comps["deployment/mongodb-orders"]["signals"]
    assert signal.startswith("no Service selects this workload") and "deployment/orders address it or call it" in signal
    assert not any(c["signals"] for cid, c in comps.items() if cid != "deployment/mongodb-orders")


def test_unnamed_connection_errors_point_along_evidence_edges_not_naming_alone():
    failing = {"classes": ["connectivity or timeout"], "count": 7, "targets": [], "line": "no backend available"}

    def run(**orders_extra):
        comps = _app(
            **{
                "deployment/orders": _server("orders", log_findings=[failing], **orders_extra),
                "deployment/mongodb-orders": _server("mongodb-orders", service=False),
            }
        )
        snap = ClusterSnapshot(app={"namespaces": [NS]}, components=comps, cluster={})
        deps.apply(snap)
        return snap, comps

    _, comps = run()
    assert "errors_point_to" not in comps["deployment/orders"]  # only a naming edge: no blame
    snap, comps = run(_calls=["deployment/mongodb-orders"])
    assert comps["deployment/orders"]["errors_point_to"] == ["deployment/mongodb-orders"]
    assert comps["deployment/mongodb-orders"]["referenced_by_errors_from"] == {
        "deployment/orders": {"connectivity or timeout": 7}
    }


def test_healthy_dependencies_take_no_blame_for_unnamed_errors():
    failing = {"classes": ["connectivity or timeout"], "count": 7, "targets": [], "line": "timeout"}
    comps = _app(**{"deployment/orders": _server("orders", log_findings=[failing])})
    snap = ClusterSnapshot(app={"namespaces": [NS]}, components=comps, cluster={})
    deps.apply(snap)
    assert "errors_point_to" not in comps["deployment/orders"]


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

    assert command_ports({"command": ["sh", "-c", "exec server --port 5000"]}) == {5000}
    assert command_ports({"args": ["--port=8080", "--listen-addr", "0.0.0.0:9090"]}) == {8080, 9090}
    assert command_ports({"args": ["--timeout=30s", "--upstream", "10.0.0.1:80", "curl localhost:5000"]}) == set()
    spec = {
        "initContainers": [{"name": "init", "command": ["nc", "-l", "-p", "5000"]}],
        "containers": [
            {"name": "app", "ports": [{"containerPort": 5000}], "args": ["--port", "5000"]},
            {"name": "sidecar", "command": ["sh", "-c", "exec server --port 5000"]},
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


def test_the_report_states_the_evidence_ranked_next_to_the_key_fact():
    from clients.jev_diag.v2.classifier import ChoiceResult, Diagnosis
    from clients.jev_diag.v2.report import build_report

    probe = "container frontend liveness probe: httpGet /health:8080 (initialDelaySeconds=0, periodSeconds=1)"
    image = "container frontend image registry.local/frontend:1.0 resources {}"
    comp = {**_server("frontend"), "signals": ["container frontend restarted 9 times"]}
    d = Diagnosis(
        component="deployment/frontend",
        component_result=ChoiceResult(choice="deployment/frontend", confidence=1.0, probabilities={}),
        fault_visible=None,
        category_result=None,
        evidence_result=ChoiceResult(choice="E1", confidence=0.5, probabilities={"E1": 0.5, "E2": 0.4, "E3": 0.1}),
        evidence=[
            {"id": "E1", "text": "container frontend restarted 9 times"},
            {"id": "E2", "text": probe},
            {"id": "E3", "text": image},
        ],
        model="m",
        input_tokens=None,
        output_tokens=None,
        investigation={"steps": []},
    )
    text = build_report(
        ClusterSnapshot(app={"namespaces": [NS]}, components={"deployment/frontend": comp}, cluster={}), d
    )
    assert "container frontend liveness probe: httpGet /health:8080" in text.splitlines()[0]
    assert "periodSeconds=1 (default 10)" in text and image not in text


def test_the_change_log_keeps_every_writer_but_core_components_and_an_objects_own_controller(monkeypatch):
    from clients.jev_diag.timeutil import parse_time, set_now
    from clients.jev_diag.v2 import outside

    def obj(kind, name, ns=None, *writes):
        fields = [
            {"manager": m, "operation": "Update", "time": t, **({"subresource": sub} if sub else {})}
            for m, t, sub in writes
        ]
        return {
            "kind": kind,
            "metadata": {
                "name": name,
                "namespace": ns,
                "creationTimestamp": "2026-10-19T07:00:00Z",
                "managedFields": fields,
            },
        }

    items = {
        "ippools.crd.projectcalico.org": [
            obj(
                "IPPool",
                "default",
                None,
                ("calico", "2026-10-19T07:00:00Z", None),
                ("kubectl-patch", "2026-10-19T08:05:00Z", None),
            )
        ],
        "nodes": [obj("Node", "worker2", None, ("kubelet", "2026-10-19T08:06:00Z", None))],
        "clusterroles.rbac.authorization.k8s.io": [
            obj("ClusterRole", "reader", None, ("helm", "2026-10-19T08:04:00Z", None))
        ],
        "widgets.example.io": [
            obj(
                "Widget",
                "w1",
                NS,
                ("widget-operator", "2026-10-19T08:05:00Z", None),
                ("widget-operator", "2026-10-19T08:05:00Z", "status"),
            )
        ],
        "configmaps": [
            obj("ConfigMap", "app-config", NS, ("kubectl-edit", "2026-10-19T08:05:00Z", None)),
            obj("ConfigMap", "dns", "kube-system", ("kubectl-client-side-apply", "2026-10-19T06:00:00Z", None)),
            obj("ConfigMap", "probe-settings", "tooling", ("kubectl", "2026-10-19T08:05:00Z", None)),
        ],
    }
    monkeypatch.setattr(outside, "run_kubectl", lambda args: " ".join(items))
    monkeypatch.setattr(outside, "kubectl_items", lambda resource, ns, errors: items[resource])
    monkeypatch.setattr(outside, "CONFIGURED_IGNORES", {"tooling"})
    set_now(parse_time("2026-10-19T08:10:00Z"))
    try:
        changes: list[dict] = []
        found = outside.change_radar([NS], _app(), [], changes, parse_time("2026-10-19T08:00:00Z"))
    finally:
        set_now(None)
    # a patch, a package manager's write and an edit are changes; a core component, an object's own controller, a
    # write made before deploy and a namespace the run leaves out are not
    assert [(f["kind"], f["name"]) for f in found] == [
        ("IPPool", "default"),
        ("ClusterRole", "reader"),
        ("ConfigMap", "app-config"),
    ]
    assert found[0]["signals"] == ["written by kubectl-patch after the application was deployed"]
    assert found[1]["signals"] == ["written by helm after the application was deployed"]


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
    assert webhook == "it intercepts CREATE pods in namespaces matching team=a"
    limit = scope(snap, diagnosis({"kind": "LimitRange", "name": "mem", "namespace": NS}))
    assert limit == (
        f"it applies to every pod created in namespace `{NS}`; `orders` also violates it, so their pods are rejected "
        "when next created"
    )
    assert scope(snap, diagnosis({"kind": "ConfigMap", "name": "x", "namespace": NS})) is None


def test_platform_failures_are_read_from_event_messages():
    from clients.jev_diag.v2.platform_events import event_signal

    webhook = event_signal(
        "FailedCreate",
        'Error creating: Internal error occurred: failed calling webhook "validate.policy.io": failed to call webhook: '
        'Post "https://policy.ops.svc:443/validate": tls: failed to verify certificate: x509: certificate signed by '
        "unknown authority",
    )
    assert webhook.startswith(
        "admission webhook validate.policy.io fails when the API server admits this workload's objects: its "
        "certificate is not trusted or not valid"
    )
    timeout = event_signal("FailedCreate", 'failed calling webhook "m.io": context deadline exceeded')
    assert "the call times out" in timeout
    sandbox = event_signal(
        "FailedCreatePodSandBox", 'plugin type="calico" failed (add): failed to allocate for range 0'
    )
    assert sandbox.startswith("pod network setup fails")
    assert event_signal("Preempted", "Preempted by pod 1234 on node worker2").startswith("pods of this workload were")
    assert event_signal("Evicted", "The node was low on resource: memory.").startswith("pods of this workload were")
    psa = event_signal(
        "FailedCreate", 'pods "x-1" is forbidden: violates PodSecurity "restricted:latest": runAsNonRoot'
    )
    assert psa.startswith("the namespace's Pod Security admission rejects this workload's pods")
    assert event_signal("BackOff", "Back-off restarting failed container") is None
    assert event_signal("FailedMount", 'configmap "x" not found') is None


def test_node_events_since_deploy_become_node_problems_linked_to_its_workloads():
    from clients.jev_diag.timeutil import parse_time
    from clients.jev_diag.v2.platform_events import node_problems

    comps = _app()
    comps["deployment/orders"]["pods"] = [{"name": "orders-1", "node": "worker2"}]
    nodes = [{"name": "worker2", "problems": []}, {"name": "worker3", "problems": []}]
    events = [
        {
            "type": "Warning",
            "reason": "EvictionThresholdMet",
            "count": 3,
            "message": "Attempting to reclaim memory",
            "lastTimestamp": "2026-10-19T08:05:00Z",
            "involvedObject": {"kind": "Node", "name": "worker2"},
        },
        {
            "type": "Normal",
            "reason": "NodeHasDiskPressure",
            "lastTimestamp": "2026-10-19T07:00:00Z",
            "involvedObject": {"kind": "Node", "name": "worker3"},
        },
    ]
    node_problems(events, nodes, comps, parse_time("2026-10-19T08:00:00Z"))
    assert nodes[0]["problems"] == ["node event EvictionThresholdMet x3: Attempting to reclaim memory"]
    assert nodes[0]["components"] == ["deployment/orders"]
    assert nodes[1]["problems"] == [] and "components" not in nodes[1]


def test_a_tls_failure_in_logs_is_a_failed_call():
    from clients.jev_diag.v2.derive import DEPENDENCY_CLASSES, LOG_CLASSES

    line = "Get https://cart:8443/items: x509: certificate has expired or is not yet valid"
    classes = [name for name, rx in LOG_CLASSES if rx.search(line)]
    assert "TLS or certificate failure" in classes and "TLS or certificate failure" in DEPENDENCY_CLASSES


def test_an_outside_change_acts_on_the_workloads_that_reference_it_not_on_its_whole_namespace():
    from clients.jev_diag.v2.outside import acting_on

    comps = {
        "deployment/web": {
            "namespace": "app",
            "name": "web",
            "template_labels": {"tier": "web"},
            "services": [{"name": "web-svc"}],
            "volumes": [],
            "service_account": "default",
        },
        "deployment/db": {
            "namespace": "app",
            "name": "db",
            "template_labels": {"tier": "db"},
            "volumes": [{"name": "data", "pvc": "db-data"}],
            "service_account": "default",
        },
    }
    assert acting_on("ConfigMap", "notes", "app", {"data": {"text": "release notes"}}, comps) == set()
    assert acting_on("Service", "web-alt", "app", {"spec": {"selector": {"tier": "web"}}}, comps) == {"deployment/web"}
    assert acting_on("PersistentVolumeClaim", "db-data", "app", {"spec": {}}, comps) == {"deployment/db"}
    hpa = {"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "web"}}}
    assert acting_on("HorizontalPodAutoscaler", "web", "app", hpa, comps) == {"deployment/web"}
    assert acting_on("Namespace", "app", None, {}, comps) == {"deployment/web", "deployment/db"}
    assert acting_on("LimitRange", "limits", "app", {}, comps) == {"deployment/web", "deployment/db"}
    assert acting_on("ConfigMap", "web", "other", {"data": {}}, comps) == set()  # another namespace


def test_login_candidates_are_ordered_by_what_explains_a_rejected_credential():
    from datetime import UTC, datetime

    from clients.jev_diag.v2.derive import server_login_rejections

    rejected = 'FATAL:  password authentication failed for user "app"'
    comps = {
        "deployment/postgresql": _server("postgresql", log_signals=[{"line": rejected, "first_seen_seconds_ago": 60}]),
        "deployment/billing": _server(
            "billing", _calls=["deployment/postgresql"], _holds_previous={"secret/billing-db": "2026-01-01T11:58:00Z"}
        ),
        "deployment/ledger": _server("ledger", _calls=["deployment/postgresql"], _env_values=["user=app host=db"]),
        "deployment/reports": _server("reports", _calls=["deployment/postgresql"]),
        "deployment/search": _server("search"),
    }
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    server_login_rejections(comps, now)
    assert comps["deployment/postgresql"]["login_candidates"] == [
        "deployment/billing",  # holds the previous values of an object rewritten before the rejections began
        "deployment/ledger",  # names the rejected account
        "deployment/reports",  # only addresses the server
    ]
    # a rewrite after the rejections began explains none of them
    comps["deployment/billing"]["_holds_previous"] = {"secret/billing-db": "2026-01-01T11:59:30Z"}
    server_login_rejections(comps, now)
    assert comps["deployment/postgresql"]["login_candidates"] == [
        "deployment/ledger",
        "deployment/billing",
        "deployment/reports",
    ]
