"""Controller contract tests; real rootless reachability remains a live gate."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from docker.errors import NotFound

from sregym.conductor.scenarios import codehub_links as links


def spec(**changes):
    fields = dict(
        source_region="region-b",
        target_region="region-a",
        group="group-0",
        public_port=22001,
        upstream_host="127.0.0.1",
        upstream_port=23001,
    )
    return links.LinkSpec(**(fields | changes))


class RelayAPI:
    def __init__(self):
        self.proxies = {}
        self.calls = []
        self.version = "2.12.0"
        self.fail = None

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if self.fail and self.fail(method, path, body):
            raise links.LinkControlError("Injected control failure", status=503)
        if path == "/version":
            return {"version": self.version}
        if path == "/proxies" and method == "GET":
            return self.proxies.copy()
        if path == "/proxies" and method == "POST":
            self.proxies[body["name"]] = body | {"toxics": {}}
            return body
        segments = path.split("/")
        proxy = self.proxies[segments[2]]
        if len(segments) == 3:
            return proxy
        if len(segments) == 4 and method == "POST":
            proxy["toxics"][body["name"]] = body
            return body
        if len(segments) == 5 and method == "DELETE":
            if proxy["toxics"].pop(segments[4], None) is None:
                raise links.LinkControlError("Missing toxic", status=404)
            return None
        raise AssertionError((method, path, body))


@pytest.fixture
def rig(monkeypatch):
    api = RelayAPI()
    container = Mock(id="captured-id")
    client = Mock()
    client.info.return_value = {"ID": "trusted-engine"}

    def create(image, **kwargs):
        container.attrs = {
            "Id": container.id,
            "Name": "/" + kwargs["name"],
            "Config": {"Labels": kwargs["labels"]},
            "State": {"Status": "running"},
        }
        return container

    client.containers.create.side_effect = create
    factory = Mock(return_value=client)
    controller = links.RegionalLinkController(
        run_id=str(uuid.uuid4()),
        trusted_host="unix:///var/run/docker.sock",
        control_bind="127.0.0.1",
        data_bind="172.17.0.1",
        control_port=18474,
        client_factory=factory,
        request=api,
    )
    monkeypatch.setattr(links, "rootless_workload_enabled", lambda: True)
    monkeypatch.setattr(links, "trusted_docker_host", lambda: controller.trusted_host)
    monkeypatch.setattr(
        links,
        "validate_rootless_boundary",
        lambda: {"runner_address": "172.17.0.1", "trusted_engine": "trusted-engine"},
    )
    monkeypatch.setattr(controller, "_check_ports", Mock())
    return SimpleNamespace(api=api, container=container, client=client, factory=factory, controller=controller)


def test_constructor_and_prepare_are_pure_and_inventory_excludes_control(rig):
    link = spec()
    rig.controller.prepare((link,), baseline=links.LinkProfile(latency_ms=10))
    rig.factory.assert_not_called()
    rig.controller._check_ports.assert_not_called()
    assert rig.api.calls == []
    assert json.loads(rig.controller.inventory_json()) == [
        {
            "source_region": "region-b",
            "target_region": "region-a",
            "group": "group-0",
            "host": "172.17.0.1",
            "port": 22001,
        }
    ]
    assert rig.controller.endpoint(link) == ("172.17.0.1", 22001)
    inventory = rig.controller.inventory_json()
    for private in ("127.0.0.1", "18474", "23001", rig.controller._owner, rig.controller.run_id, "toxiproxy", "sregym"):
        assert private not in inventory


@pytest.mark.parametrize(
    "changes",
    [
        {"source_region": "region-a"},
        {"source_region": "Region-B"},
        {"group": "../group"},
        {"group": "a" * 33},
        {"public_port": True},
        {"public_port": 0},
        {"public_port": 1023},
        {"public_port": 32768},
        {"upstream_port": "3306"},
        {"upstream_host": "localhost"},
        {"upstream_host": "0.0.0.0"},
        {"upstream_host": "224.0.0.1"},
        {"upstream_host": "::1"},
    ],
)
def test_malformed_link_is_rejected(changes):
    with pytest.raises(ValueError):
        spec(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"latency_ms": True},
        {"latency_ms": 5001},
        {"jitter_ms": 1},
        {"jitter_ms": -1},
        {"bandwidth_kbps": 0},
        {"bandwidth_kbps": 64001},
        {"bandwidth_kbps": 1.5},
        {"reset_timeout_ms": -1},
        {"reset_timeout_ms": 10001},
    ],
)
def test_profiles_are_bounded(changes):
    with pytest.raises(ValueError):
        links.LinkProfile(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"control_bind": "0.0.0.0"},
        {"control_bind": "172.17.0.1"},
        {"data_bind": "127.0.0.1"},
        {"trusted_host": "tcp://host:2375"},
        {"trusted_host": ""},
        {"control_port": 80},
        {"readiness_seconds": 61},
    ],
)
def test_control_and_data_require_explicit_distinct_bindings(changes):
    fields = dict(
        run_id=str(uuid.uuid4()),
        trusted_host="unix:///var/run/docker.sock",
        control_bind="127.0.0.1",
        data_bind="172.17.0.1",
        control_port=18474,
    )
    with pytest.raises(ValueError):
        links.RegionalLinkController(**(fields | changes))


@pytest.mark.parametrize(
    "specs",
    [
        (),
        [spec()],
        (spec(), spec()),
        (spec(public_port=18474),),
        (spec(upstream_port=18474),),
        (spec(upstream_port=22001),),
        (spec(upstream_host="10.0.0.4"),),
        (spec(),) * (links.MAX_LINKS + 1),
    ],
)
def test_listener_collision_remote_upstream_and_unbounded_inventory_rejected(rig, specs):
    with pytest.raises((ValueError, TypeError)):
        rig.controller.prepare(specs)
    rig.factory.assert_not_called()


def test_trusted_container_uses_actual_separate_listeners_and_strict_resources(rig):
    rig.controller.start((spec(),))
    rig.factory.assert_called_once_with("unix:///var/run/docker.sock")
    args, kwargs = rig.client.containers.create.call_args
    assert args == (links.TOXIPROXY_IMAGE,)
    assert "@sha256:" in args[0]
    assert kwargs["network_mode"] == "host"
    assert kwargs["command"] == ["-host", "127.0.0.1", "-port", "18474"]
    assert kwargs["user"] == "65532:65532"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["read_only"] and not kwargs["privileged"]
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["mem_limit"] == kwargs["memswap_limit"] == 256 * 1024 * 1024
    assert kwargs["pids_limit"] == 64
    assert kwargs["restart_policy"] == {"Name": "no"}
    assert not {"volumes", "mounts", "ports", "pid_mode", "devices"}.intersection(kwargs)
    proxy = next(iter(rig.api.proxies.values()))
    assert proxy["listen"] == "172.17.0.1:22001"
    assert proxy["upstream"] == "127.0.0.1:23001"
    rig.controller.start()
    assert rig.client.containers.create.call_count == 1


def test_both_stream_directions_and_each_regional_direction_are_explicit(rig):
    forward = spec()
    reverse = spec(source_region="region-a", target_region="region-b", public_port=22002, upstream_port=23002)
    rig.controller.start((forward, reverse))
    profile = links.LinkProfile(latency_ms=20, jitter_ms=2, bandwidth_kbps=256, reset_timeout_ms=0)
    for link in (forward, reverse):
        rig.controller.apply_profile(link, profile)
        toxics = rig.api.proxies[rig.controller._proxy_name(link)]["toxics"].values()
        assert len(toxics) == 6
        assert {(toxic["type"], toxic["stream"]) for toxic in toxics} == {
            (kind, stream) for kind in ("latency", "bandwidth", "reset_peer") for stream in ("upstream", "downstream")
        }
        assert all(toxic["toxicity"] == 1.0 for toxic in toxics)
        assert [toxic["attributes"] for toxic in toxics if toxic["type"] == "bandwidth"] == [{"rate": 256}] * 2
        assert [toxic["attributes"] for toxic in toxics if toxic["type"] == "reset_peer"] == [{"timeout": 0}] * 2


def test_restore_preserves_healthy_baseline_and_unowned_toxic(rig):
    link = spec()
    rig.controller.start((link,), baseline=links.LinkProfile(latency_ms=10))
    proxy = rig.api.proxies[rig.controller._proxy_name(link)]
    proxy["toxics"]["foreign"] = {"name": "foreign"}
    rig.controller.apply_profile(link, links.LinkProfile(latency_ms=1000, bandwidth_kbps=10))
    rig.controller.restore()
    assert "foreign" in proxy["toxics"]
    owned = [toxic for name, toxic in proxy["toxics"].items() if name != "foreign"]
    assert len(owned) == 2 and all(t["attributes"] == {"latency": 10, "jitter": 0} for t in owned)
    assert all(path != "/reset" and path != "/populate" for _, path, _ in rig.api.calls)


def test_partial_profile_failure_remains_visible_and_can_be_restored(rig):
    link = spec()
    rig.controller.start((link,))
    rig.api.fail = lambda method, path, body: method == "POST" and body.get("stream") == "downstream"
    with pytest.raises(links.LinkControlError):
        rig.controller.apply_profile(link, links.LinkProfile(latency_ms=200))
    assert len(rig.controller._toxics[link]) == 2
    rig.api.fail = None
    rig.controller.restore(link)
    assert not rig.controller._toxics[link]
    assert not rig.api.proxies[rig.controller._proxy_name(link)]["toxics"]


def test_ownership_change_blocks_mutation_and_cleanup(rig):
    link = spec()
    rig.controller.start((link,))
    rig.container.attrs["Config"]["Labels"][links.OWNER_LABEL] = "replacement-owner"
    for action in (lambda: rig.controller.apply_profile(link, links.LinkProfile(latency_ms=2)), rig.controller.stop):
        with pytest.raises(links.LinkControlError, match="ownership"):
            action()
    rig.container.remove.assert_not_called()
    rig.client.close.assert_not_called()


def test_endpoint_change_blocks_toxic_mutation(rig):
    link = spec()
    rig.controller.start((link,))
    rig.api.proxies[rig.controller._proxy_name(link)]["upstream"] = "127.0.0.1:9999"
    with pytest.raises(links.LinkControlError, match="configuration changed"):
        rig.controller.apply_profile(link, links.LinkProfile(latency_ms=2))
    assert not any(path.endswith("/toxics") for _, path, _ in rig.api.calls)


def test_partial_start_cleans_only_captured_owned_container(rig):
    rig.api.fail = lambda method, path, body: method == "POST" and path == "/proxies"
    with pytest.raises(links.LinkControlError):
        rig.controller.start((spec(),))
    rig.container.remove.assert_called_once_with(force=True)
    rig.client.close.assert_called_once()
    rig.client.containers.list.assert_not_called()
    rig.client.containers.prune.assert_not_called()


def test_stop_is_idempotent_and_missing_id_does_not_delete_by_name(rig):
    rig.controller.start((spec(),))
    rig.container.reload.side_effect = NotFound("captured container absent")
    rig.controller.stop()
    rig.controller.stop()
    rig.container.remove.assert_not_called()
    rig.client.containers.get.assert_not_called()
    rig.client.close.assert_called_once()


def test_cleanup_failure_remains_retryable(rig):
    rig.controller.start((spec(),))
    rig.container.remove.side_effect = RuntimeError("engine temporarily unavailable")
    with pytest.raises(RuntimeError):
        rig.controller.stop()
    rig.client.close.assert_not_called()
    rig.container.remove.side_effect = None
    rig.controller.stop()
    assert rig.container.remove.call_count == 2
    rig.client.close.assert_called_once()


def test_empty_fresh_proxy_inventory_is_required(rig):
    rig.api.proxies["existing"] = {"name": "existing"}
    with pytest.raises(links.LinkControlError, match="empty"):
        rig.controller.start((spec(),))
    assert all(method != "POST" for method, _, _ in rig.api.calls)
    rig.container.remove.assert_called_once_with(force=True)


@pytest.mark.parametrize("failure", ["disabled", "host", "bind", "engine"])
def test_boundary_mismatch_has_no_fallback(rig, monkeypatch, failure):
    if failure == "disabled":
        monkeypatch.setattr(links, "rootless_workload_enabled", lambda: False)
    elif failure == "host":
        monkeypatch.setattr(links, "trusted_docker_host", lambda: "unix:///run/user/20041/docker.sock")
    elif failure == "bind":
        monkeypatch.setattr(
            links,
            "validate_rootless_boundary",
            lambda: {"runner_address": "10.0.0.4", "trusted_engine": "trusted-engine"},
        )
    else:
        rig.client.info.return_value = {"ID": "workload-engine"}
    with pytest.raises(links.LinkControlError):
        rig.controller.start((spec(),))
    rig.client.containers.create.assert_not_called()


def test_http_transport_disables_environment_proxies_and_bounds_reads(rig, monkeypatch):
    rig.controller.start((spec(),))
    rig.controller._request_override = None
    response = Mock()
    response.read.return_value = b'{"version":"2.12.0"}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr(links.urllib.request, "build_opener", build)
    assert rig.controller._request("GET", "/version") == {"version": "2.12.0"}
    assert build.call_args.args[0].proxies == {}
    assert isinstance(build.call_args.args[1], links._NoRedirect)
    response.read.assert_called_once_with(links.MAX_RESPONSE_BYTES + 1)
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:18474/version"
    assert opener.open.call_args.kwargs["timeout"] == 3
    response.read.return_value = b" " * (links.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(links.LinkControlError, match="exceeded"):
        rig.controller._request("GET", "/version")


def test_control_redirect_is_rejected():
    with pytest.raises(links.LinkControlError, match="redirect"):
        links._NoRedirect().redirect_request(None, None, 302, "", {}, "http://outside.invalid/")
