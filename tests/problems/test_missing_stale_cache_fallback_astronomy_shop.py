import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from kubernetes.client.rest import ApiException

import sregym.conductor.problems.missing_stale_cache_fallback_astronomy_shop as problem_module
from sregym.conductor.oracles.missing_stale_cache_fallback_mitigation import ProbeUnavailable
from sregym.conductor.problems.missing_stale_cache_fallback_astronomy_shop import MissingStaleCacheFallbackAstronomyShop


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def problem():
    problem = MissingStaleCacheFallbackAstronomyShop.__new__(MissingStaleCacheFallbackAstronomyShop)
    problem.namespace = "astronomy-shop"
    problem.delay = "3s"
    problem.delay_seconds = 3.0
    problem.delay_active_min_s = 2.4
    problem._client = None
    problem._client_image = None
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


def test_delay_length_can_be_overridden(problem):
    problem.apply_delay(keep_alive=False, latency="60s")
    manifest = yaml.safe_load(problem.kubectl.exec_command_checked.call_args.kwargs["input_data"])
    assert manifest["spec"]["delay"]["latency"] == "60s"


CRDS = "crd/schedules.chaos-mesh.org\ncrd/networkchaos.chaos-mesh.org\n"


def test_chaos_delete_is_checked_bounded_and_uses_full_resource_names(problem):
    problem.kubectl.exec_command_checked = Mock(side_effect=[CRDS, "", ""])
    problem._delete_chaos()
    delete = problem.kubectl.exec_command_checked.call_args_list[1].args[0]
    assert "schedules.chaos-mesh.org,networkchaos.chaos-mesh.org catalog-latency -n astronomy-shop" in delete
    assert "--timeout=60s" in delete
    problem.kubectl.exec_command.assert_not_called()


def test_chaos_delete_skips_a_cluster_without_chaos_mesh(problem):
    problem._delete_chaos()
    assert problem.kubectl.exec_command_checked.call_count == 1


def test_chaos_delete_failure_is_reported(problem):
    problem.kubectl.exec_command_checked = Mock(side_effect=[CRDS, RuntimeError("Error from server (Forbidden)")])
    with pytest.raises(RuntimeError, match="Forbidden"):
        problem._delete_chaos()


def test_chaos_that_outlives_the_delete_is_reported(problem, monkeypatch):
    monkeypatch.setattr(problem_module, "time", Clock())
    left = "networkchaos.chaos-mesh.org/catalog-latency-x7k2p\n"
    problem.kubectl.exec_command_checked = Mock(side_effect=lambda command, **kw: CRDS if "crd" in command else left)
    with pytest.raises(RuntimeError, match="still present"):
        problem._delete_chaos()


def test_chaos_stuck_on_its_finalizer_is_stripped_and_checked(problem, monkeypatch):
    # Seen in CI: Chaos Mesh couldn't apply a delay, and the object then never finished deleting.
    monkeypatch.setattr(problem_module, "time", Clock())
    stuck = "networkchaos.chaos-mesh.org/catalog-latency\n"
    listings = iter([stuck, stuck, ""])

    def run(command, **kwargs):
        if "get crd" in command:
            return CRDS
        if command.startswith("kubectl delete"):
            raise RuntimeError("error: timed out waiting for the condition on networkchaos/catalog-latency")
        if command.startswith("kubectl get"):
            return next(listings)
        return ""

    problem.kubectl.exec_command_checked = Mock(side_effect=run)
    problem._delete_chaos()
    patches = [c.args[0] for c in problem.kubectl.exec_command_checked.call_args_list if "kubectl patch" in c.args[0]]
    assert patches == [
        "kubectl patch networkchaos.chaos-mesh.org/catalog-latency -n astronomy-shop --type merge "
        '-p \'{"metadata": {"finalizers": []}}\''
    ]


def test_failed_chaos_cleanup_fails_recovery(problem):
    problem._delete_chaos = Mock(side_effect=RuntimeError("Error from server (Forbidden)"))
    with pytest.raises(RuntimeError, match="Forbidden"):
        problem.recover_fault()


def test_reference_fix_that_cannot_be_applied_does_not_fail_teardown(problem):
    # The agent may have scaled recommendation to 0 or deleted it. Raising here would abort the whole run.
    problem._delete_chaos = Mock()
    problem._namespace_exists = Mock(return_value=True)
    problem._fixed_source = "fixed"
    problem._overlay = Mock(side_effect=RuntimeError('deployments.apps "recommendation" not found'))
    problem.release_client = Mock()
    assert problem.recover_fault() is False
    problem.release_client.assert_called_once()


def test_incident_state_read_is_checked(problem):
    problem.kubectl.exec_command_checked = Mock(side_effect=[CRDS, RuntimeError("Error from server (Forbidden)")])
    with pytest.raises(RuntimeError, match="Forbidden"):
        problem.incident_running()


DEADLINE = {
    "ok": False,
    "ms": 1003.0,
    "code": "UNKNOWN",
    "error": "Exception calling application: StatusCode.DEADLINE_EXCEEDED",
}


@pytest.mark.parametrize(
    "results,confirmed",
    [
        ([DEADLINE] * 3, True),
        ([], False),
        ([{"ok": False, "ms": 0.0, "code": "EXEC_FAILED", "error": "Forbidden"}], False),
        ([DEADLINE, {"ok": False, "ms": 0.0, "code": "NO_OUTPUT", "error": ""}], False),
        ([DEADLINE, {"ok": True, "ms": 5.0, "ids": ["66VCHSJNUP"]}], False),
        ([{"ok": False, "ms": 3.0, "code": "UNAVAILABLE", "error": "Connection refused"}], False),
    ],
)
def test_injection_needs_answers_that_show_the_upstream_deadline(results, confirmed):
    assert MissingStaleCacheFallbackAstronomyShop._incident_confirmed(results) is confirmed


def test_probes_call_the_service_from_the_client_pod(problem):
    problem._exec_in_client = Mock(return_value='{"ok": true, "ms": 4.0, "ids": []}\n')
    problem._exec_in_rollout = Mock()
    problem.probe_recommendations(1, 0, 5.0)
    assert problem._exec_in_client.call_args.args[1][-1] == "recommendation:8080"
    problem._exec_in_rollout.assert_not_called()


def test_pod_probes_call_that_pod_directly(problem):
    problem._exec_in_rollout = Mock(return_value='{"ok": true, "ms": 4.0, "ids": []}\n')
    problem.probe_recommendations(1, 0, 5.0, pod="recommendation-abc")
    assert problem._exec_in_rollout.call_args.args[1][-1] == "localhost:8080"
    assert problem._exec_in_rollout.call_args.kwargs["pod"] == "recommendation-abc"


def test_rollout_exec_ignores_the_containers_python_environment(problem):
    problem._recommendation_pod = Mock(return_value="recommendation-abc")
    problem._exec_in_rollout("print(1)", ["product-catalog:8080", "14"], timeout=30)
    command = problem.kubectl.exec_command_checked.call_args.args[0]
    assert "/venv/bin/python -I -" in command


def test_client_pod_runs_the_recorded_image_without_app_labels(problem):
    problem._client_image = "ghcr.io/open-telemetry/demo:2.2.0-recommendation"
    problem._rollout_pod_objects = Mock(return_value=[SimpleNamespace(spec=SimpleNamespace(node_name="kind-worker"))])
    name = problem._client_pod()
    manifest = json.loads(problem.kubectl.exec_command_checked.call_args_list[0].kwargs["input_data"])
    assert manifest["metadata"]["name"] == name and "labels" not in manifest["metadata"]
    assert manifest["spec"]["containers"][0]["image"] == "ghcr.io/open-telemetry/demo:2.2.0-recommendation"
    assert manifest["spec"]["nodeName"] == "kind-worker"
    assert problem._client_pod() == name  # started once
    problem.release_client()
    assert f"delete pod {name}" in problem.kubectl.exec_command_checked.call_args.args[0]
    assert problem._client is None


def _run_probe_script(monkeypatch, exclude):
    """Run _PROBE_SCRIPT against fake grpc modules and return the product_ids it sent."""
    sent = []

    class Stub:
        def __init__(self, channel):
            pass

        def ListRecommendations(self, request, timeout):
            sent.append(request["product_ids"])
            return SimpleNamespace(product_ids=[])

    fake_grpc = types.ModuleType("grpc")
    fake_grpc.insecure_channel = lambda address: SimpleNamespace(close=lambda: None)
    fake_grpc.RpcError = Exception
    fake_pb2 = types.ModuleType("demo_pb2")
    fake_pb2.ListRecommendationsRequest = lambda product_ids: {"product_ids": product_ids}
    fake_pb2_grpc = types.ModuleType("demo_pb2_grpc")
    fake_pb2_grpc.RecommendationServiceStub = Stub
    for name, module in (("grpc", fake_grpc), ("demo_pb2", fake_pb2), ("demo_pb2_grpc", fake_pb2_grpc)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "argv", ["-", "1", "0", "5", exclude, "recommendation:8080"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(compile(problem_module._PROBE_SCRIPT, "probe", "exec"), {"__name__": "__main__"})
    return sent[0]


def test_exclusions_read_the_same_whichever_way_the_service_parses_them(monkeypatch, capsys):
    product_ids = _run_probe_script(monkeypatch, "AAA,BBB,CCC")
    capsys.readouterr()
    stock = "".join(product_ids).split(",")
    as_list = {i for i in product_ids}
    split_each = {part for item in product_ids for part in item.split(",")}
    assert stock == ["AAA", "BBB", "CCC"]
    assert {"AAA", "BBB", "CCC"} <= as_list
    assert {"AAA", "BBB", "CCC"} <= split_each


def _meta(name, uid, owner=None, revision=None, created=0, deleting=False):
    return SimpleNamespace(
        name=name,
        uid=uid,
        owner_references=[SimpleNamespace(uid=owner)] if owner else None,
        annotations={"deployment.kubernetes.io/revision": revision} if revision else None,
        creation_timestamp=created,
        deletion_timestamp=1 if deleting else None,
    )


def test_probe_pod_belongs_to_the_current_rollout(problem):
    deployment = SimpleNamespace(
        metadata=_meta("recommendation", "dep", revision="3"),
        spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"opentelemetry.io/name": "recommendation"})),
    )
    replica_sets = [
        _meta("rs-new", "rs3", owner="dep", revision="3"),
        _meta("rs-old", "rs2", owner="dep", revision="2"),
    ]
    pods = [
        _meta("current-older", "p0", owner="rs3", created=0),
        _meta("current", "p1", owner="rs3", created=1),
        _meta("old-rollout", "p2", owner="rs2", created=2),
        _meta("stray", "p3", created=3),
        _meta("terminating", "p4", owner="rs3", created=4, deleting=True),
    ]
    problem.kubectl.apps_v1_api = SimpleNamespace(
        read_namespaced_deployment=Mock(return_value=deployment),
        list_namespaced_replica_set=Mock(
            return_value=SimpleNamespace(items=[SimpleNamespace(metadata=m) for m in replica_sets])
        ),
    )
    problem.kubectl.core_v1_api = SimpleNamespace(
        list_namespaced_pod=Mock(
            return_value=SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=m, status=SimpleNamespace(phase="Running", container_statuses=[]), spec=None
                    )
                    for m in pods
                ]
            )
        )
    )
    assert problem._recommendation_pod() == "current"
    assert problem.rollout_pods() == ["current-older", "current"]


def test_routing_specs_report_services_and_name_resolution(problem):
    from kubernetes.client import V1HostAlias

    def read(name, namespace):
        if name == "product-catalog":
            raise ApiException(status=404, reason="Not Found")
        port = SimpleNamespace(port=8080, target_port=8080, protocol="TCP")
        spec = SimpleNamespace(selector={"opentelemetry.io/name": name}, ports=[port], session_affinity="None")
        return SimpleNamespace(spec=spec)

    pod_spec = SimpleNamespace(
        host_aliases=[V1HostAlias(ip="10.96.34.212", hostnames=["product-catalog"])],
        dns_policy="ClusterFirst",
        dns_config=None,
    )
    deployment = SimpleNamespace(spec=SimpleNamespace(template=SimpleNamespace(spec=pod_spec)))
    problem.kubectl.core_v1_api = SimpleNamespace(read_namespaced_service=read)
    problem.kubectl.apps_v1_api = SimpleNamespace(read_namespaced_deployment=Mock(return_value=deployment))
    assert problem.routing_specs() == {
        "recommendation": {
            "selector": {"opentelemetry.io/name": "recommendation"},
            "ports": [(8080, "8080", "TCP")],
            "session_affinity": "None",
        },
        "product-catalog": None,
        "recommendation-dns": {
            "host_aliases": [{"ip": "10.96.34.212", "hostnames": ["product-catalog"]}],
            "dns_policy": "ClusterFirst",
            "dns_config": None,
        },
    }


def _pod(ip, phase="Running", deleting=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(deletion_timestamp=1 if deleting else None),
        status=SimpleNamespace(phase=phase, pod_ip=ip),
    )


def test_upstream_is_timed_at_a_product_catalog_pod_ip(problem):
    pods = [_pod("10.244.1.7"), _pod("10.244.2.9", deleting=True), _pod(None, phase="Pending")]
    problem.kubectl.core_v1_api = SimpleNamespace(list_namespaced_pod=Mock(return_value=SimpleNamespace(items=pods)))
    assert problem.upstream_addrs() == ["10.244.1.7:8080"]
    assert "opentelemetry.io/name=product-catalog" in str(problem.kubectl.core_v1_api.list_namespaced_pod.call_args)

    problem._exec_in_rollout = Mock(return_value='{"addr": "10.244.1.7:8080", "s": 3.01, "code": "OK"}\n')
    assert problem.measure_upstream()["s"] == 3.01
    assert problem._exec_in_rollout.call_args.args[1][0] == "10.244.1.7:8080"


def test_upstream_without_a_running_pod_cannot_be_timed(problem):
    problem.upstream_addrs = Mock(return_value=[])
    assert problem.measure_upstream() is None


def test_frontend_probes_ask_the_frontend_from_the_client_pod(problem):
    problem._exec_in_client = Mock(return_value='{"ok": true, "ms": 40.0, "ids": ["1YMWWN1N4O"]}\n')
    assert problem.probe_frontend(1, 0, 7.0, "0PUK6V6EV0") == [{"ok": True, "ms": 40.0, "ids": ["1YMWWN1N4O"]}]
    script, args = problem._exec_in_client.call_args.args[:2]
    assert args[-2:] == ["0PUK6V6EV0", "frontend:8080"]
    assert "/api/recommendations?productIds=" in script


def test_frontend_probe_that_cannot_run_is_reported(problem):
    problem._exec_in_client = Mock(side_effect=RuntimeError("pods is forbidden"))
    assert problem.probe_frontend(1, 0, 7.0, "0PUK6V6EV0")[0]["code"] == "EXEC_FAILED"


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


def test_wait_for_delay_needs_an_explicit_answer(problem, monkeypatch):
    monkeypatch.setattr(problem_module, "time", Clock())
    problem.delay_active = Mock(return_value=None)
    assert problem.wait_for_delay(active=False) is False


def test_wait_until_answering_waits_while_nothing_is_behind_the_service(problem, monkeypatch):
    monkeypatch.setattr(problem_module, "time", Clock())
    refused = {
        "ok": False,
        "ms": 1.0,
        "code": "UNAVAILABLE",
        "error": "failed to connect to all addresses; last error: UNKNOWN: ipv4:10.96.1.2:8080: Connection refused",
    }
    problem.probe_recommendations = Mock(side_effect=[[refused], [refused], [{"ok": True, "ms": 5.0, "ids": []}]])
    problem.wait_until_answering()
    assert problem.probe_recommendations.call_count == 3


def test_probes_that_never_run_are_reported_as_unavailable(problem, monkeypatch):
    monkeypatch.setattr(problem_module, "time", Clock())
    problem.probe_recommendations = Mock(return_value=[{"ok": False, "ms": 0.0, "code": "EXEC_FAILED", "error": "x"}])
    with pytest.raises(ProbeUnavailable):
        problem.wait_until_answering()
