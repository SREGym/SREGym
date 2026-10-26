import contextlib
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner, ExecInput
from sregym.service.endpoint_access import EndpointAccess, EndpointGateway
from sregym.service.internet_policy import EndpointRule, InternetPolicy


@contextlib.contextmanager
def server(handler):
    instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=instance.serve_forever)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        data = self.path.encode() + b"x" * (2**20)
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(201)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_gateway_streams_uploads_and_downloads_without_rewriting_target():
    with server(Echo) as upstream:
        gateway = EndpointGateway("127.0.0.1", [upstream.server_port], 8000)
        gateway.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=5)
            connection.request("GET", f"/{upstream.server_port}/git/repo?key=value")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == b"/git/repo?key=value" + b"x" * (2**20)
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=5)
            payload = b"ordinary export\0" * 100000
            connection.request("POST", f"/{upstream.server_port}/upload", body=payload)
            response = connection.getresponse()
            assert response.status == 201
            assert response.read() == payload
            connection.close()
        finally:
            gateway.close()


@pytest.mark.parametrize(
    "path,method",
    [
        ("/22/", "GET"),
        ("/28301/", "GET"),
        ("/22421/", "GET"),
        ("/build/", "GET"),
        ("/8000/admin", "GET"),
        ("/8000/submit", "GET"),
        ("/8000/get_app", "POST"),
        ("http://127.0.0.1:22/", "GET"),
    ],
)
def test_gateway_has_no_other_host_destinations(path, method):
    gateway = EndpointGateway("127.0.0.1", [28311], 8000)
    try:
        assert gateway.destination(path, method) is None
    finally:
        gateway.server_close()


def test_deadline_blocks_company_actions_but_preserves_conductor_status():
    gateway = EndpointGateway("127.0.0.1", [28311], 8000, is_open=lambda: False)
    try:
        assert gateway.destination("/28311/exec", "POST") is None
        assert gateway.destination("/8000/submit", "POST") is None
        assert gateway.destination("/8000/status", "GET")[:3] == ("127.0.0.1", 8000, "/status")
    finally:
        gateway.server_close()


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "empty-home")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    access = EndpointAccess(
        tmp_path / "public", "http://host.docker.internal:39483", tmp_path / "container.json", time.monotonic() + 1800
    )
    runner = ContainerRunner(
        ContainerConfig(
            endpoint_access=access,
            internet_policy=InternetPolicy.from_mode(
                "filtered", agent_name="autosubmit", additional_allowed_endpoints=["http://host.docker.internal:22"]
            ),
            kubeconfig_path=tmp_path / "config",
            sregym_apps_path=tmp_path,
            sregym_app_subdirs=["private"],
            codex_auth="none",
            forward_host_credentials=False,
        )
    )
    runner._egress.ensure_started = Mock()
    runner._egress.docker_args = Mock(return_value=["--network=private-evaluation"])
    runner._egress.environment = Mock(return_value={})
    runner.config.kubeconfig_path.touch()
    (tmp_path / "private").mkdir()
    return runner


def test_application_access_replaces_kubernetes_mcp_and_optional_host_rules(runner):
    assert runner._egress.disable_bridge_address is True
    assert runner._configured_egress_rules({}) == (EndpointRule("host.docker.internal", 39483, inspect_tools=False),)


def test_container_gets_only_public_tools_and_its_own_workspace(runner, tmp_path):
    command = runner.build_docker_command(ExecInput("python -m clients.autosubmit.autosubmit_agent", label="test"))
    assert "--network=private-evaluation" in command
    assert "--network=host" not in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    volumes = [command[i + 1] for i, value in enumerate(command) if value == "-v"]
    assert volumes == [f"{tmp_path / 'public'}:/opt/company:ro"]
    assert "/root/.kube/config" not in " ".join(command)
    assert "endpoint_access.py --" in command[-1]
    assert json.loads((tmp_path / "container.json").read_text())["name"].startswith("sregym-test-")


def test_cannot_launch_after_deadline(runner):
    old = runner.config.endpoint_access
    runner.config.endpoint_access = EndpointAccess(old.public_dir, old.gateway_url, old.container_record, 0)
    with pytest.raises(TimeoutError, match="deadline has passed"):
        runner.build_docker_command(ExecInput("echo no", label="test"))
    assert not old.container_record.exists()


def test_codex_sessions_are_persisted_before_independent_container_cutoff(runner, tmp_path):
    runner.config.logs_path = tmp_path / "logs"
    runner.config.internet_policy = InternetPolicy.from_mode("filtered", agent_name="codex")
    command = runner.build_docker_command(ExecInput("true", label="test"))
    volumes = [command[i + 1] for i, value in enumerate(command) if value == "-v"]
    assert f"{tmp_path / 'logs/sessions'}:/root/.codex/sessions" in volumes
    assert (tmp_path / "logs/sessions").is_dir()


@pytest.mark.parametrize("mode,harden", [("open", True), ("filtered", False)])
def test_application_access_requires_filtered_hardened_container(tmp_path, mode, harden):
    runner = ContainerRunner(ContainerConfig(internet_policy=InternetPolicy.from_mode(mode), harden_container=harden))
    with pytest.raises(RuntimeError, match="requires filtered"):
        runner.set_endpoint_access(EndpointAccess(tmp_path, "http://example.test", tmp_path / "name", 1800))
