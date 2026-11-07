import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from kubernetes import client

from sregym.conductor.problems.edge_request_filter_cpu_saturation import (
    EdgeRequestFilterCPUSaturation,
)


def _template(command, *, annotations=None):
    return client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(
            labels={"app.kubernetes.io/name": "frontend-proxy"},
            annotations=annotations or {},
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="frontend-proxy",
                    image="frontend-proxy:latest",
                    command=command,
                )
            ]
        ),
    )


def _deployment(template, *, replicas=2, strategy="agent-strategy"):
    return SimpleNamespace(
        spec=SimpleNamespace(
            template=template,
            replicas=replicas,
            strategy=strategy,
        )
    )


class _AppsV1:
    def __init__(self):
        self.replacements = []

    def replace_namespaced_deployment(self, name, namespace, body):
        self.replacements.append((name, namespace, body))


class _KubeCtl:
    def __init__(self, deployment):
        self.deployment = deployment
        self.apps_v1_api = _AppsV1()
        self.commands = []

    def get_deployment(self, name, namespace):
        return self.deployment

    def exec_command_checked(self, command, timeout=None):
        self.commands.append(command)
        return ""


def _problem(kubectl):
    problem = EdgeRequestFilterCPUSaturation.__new__(EdgeRequestFilterCPUSaturation)
    problem.kubectl = kubectl
    problem.namespace = "astronomy-shop"
    problem.faulty_service = "frontend-proxy"
    problem.traffic_source = "load-generator"
    problem.process_marker = "edge-traffic-replay"
    problem.driver_log = "/tmp/edge-traffic-replay.log"
    problem.driver_pid = "/tmp/edge-traffic-replay.pid"
    problem._baseline_template = None
    return problem


def test_capture_keeps_an_independent_copy_of_the_baseline_template():
    original = _template(None, annotations={"baseline": "true"})
    problem = _problem(_KubeCtl(_deployment(original)))

    problem._capture_baseline_template()
    original.metadata.annotations["baseline"] = "mutated"
    original.spec.containers[0].command = ["faulted"]

    assert problem._baseline_template.metadata.annotations["baseline"] == "true"
    assert problem._baseline_template.spec.containers[0].command is None


def test_capture_does_not_overwrite_the_original_baseline_on_repeated_injection():
    original = _template(None)
    kubectl = _KubeCtl(_deployment(original))
    problem = _problem(kubectl)
    problem._capture_baseline_template()

    kubectl.deployment.spec.template = _template(["faulted"])
    problem._capture_baseline_template()

    assert problem._baseline_template.spec.containers[0].command is None


def test_restore_replaces_only_template_on_latest_deployment():
    baseline = _template(None, annotations={"baseline": "true"})
    current = _deployment(
        _template(["agent-rollout"], annotations={"agent": "kept-outside-template"}),
        replicas=3,
        strategy="agent-strategy",
    )
    kubectl = _KubeCtl(current)
    problem = _problem(kubectl)
    problem._baseline_template = baseline

    problem._restore_baseline_template()

    assert len(kubectl.apps_v1_api.replacements) == 1
    name, namespace, replacement = kubectl.apps_v1_api.replacements[0]
    assert (name, namespace) == ("frontend-proxy", "astronomy-shop")
    assert replacement.spec.template.metadata.annotations == {"baseline": "true"}
    assert replacement.spec.template.spec.containers[0].command is None
    assert replacement.spec.replicas == 3
    assert replacement.spec.strategy == "agent-strategy"


def test_recovery_restores_snapshot_instead_of_undoing_previous_revision():
    baseline = _template(None)
    kubectl = _KubeCtl(_deployment(_template(["agent-rollout"])))
    problem = _problem(kubectl)
    problem._baseline_template = baseline

    problem.recover_fault()

    assert len(kubectl.apps_v1_api.replacements) == 1
    assert not any("rollout undo" in command for command in kubectl.commands)
    assert any("rollout status deployment/frontend-proxy" in command for command in kubectl.commands)
    cleanup_command = next(command for command in kubectl.commands if "kubectl exec" in command)
    assert "/proc" in cleanup_command
    assert "signal.SIGTERM" in cleanup_command
    assert problem._baseline_template is None
    assert problem.fault_injected is False


def test_restore_requires_a_captured_baseline():
    problem = _problem(_KubeCtl(_deployment(_template(["faulted"]))))

    try:
        problem._restore_baseline_template()
    except RuntimeError as exc:
        assert "captured baseline" in str(exc)
    else:
        raise AssertionError("Expected recovery without a baseline to fail")


def test_python_program_is_not_expanded_by_the_host_shell(tmp_path):
    import sys

    stub = tmp_path / "kubectl"
    stub.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    stub.chmod(0o755)
    kubectl = _KubeCtl(None)
    kubectl.exec_command_checked = lambda command, **kwargs: subprocess.check_output(
        command, shell=True, text=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PY": "host"}
    )
    problem = _problem(kubectl)
    argv = json.loads(problem._traffic_python("print('literal $PY $(false) $!')"))
    assert argv[:2] == ["exec", "deployment/load-generator"]
    output = subprocess.check_output(argv[argv.index("--") + 1 :], text=True)
    assert output.strip() == "literal $PY $(false) $!"


def test_traffic_launch_program_uses_a_detached_checked_child():
    from unittest.mock import Mock

    problem = _problem(None)
    problem.crafted_payload = "abc!"
    problem.driver_interval_seconds = 0.05
    problem._traffic_python = Mock()
    problem._start_crafted_traffic()
    source = problem._traffic_python.call_args.args[0]
    compile(source, "traffic-launcher", "exec")
    assert "subprocess.Popen" in source
    assert "start_new_session=True" in source
    assert "process.poll()" in source
    assert "http://frontend-proxy:8080/?waf=abc!" in source


@pytest.mark.skipif(shutil.which("perl") is None, reason="Perl is not installed")
def test_safe_edge_proxy_preserves_post_headers_and_body(free_tcp_port):
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append((self.path, self.headers.get("Host"), body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    problem = _problem(None)
    problem.regex_env, problem.bad_regex = "WAF_RULE_REGEX", "^([a-zA-Z]+)*$"
    upstream_port = server.server_address[1]
    process = subprocess.Popen(
        ["perl", "-e", problem._edge_filter_script()],
        env={
            **os.environ,
            "ENVOY_PORT": str(free_tcp_port),
            "FRONTEND_HOST": "127.0.0.1",
            "FRONTEND_PORT": str(upstream_port),
            "WAF_RULE_REGEX": "^[A-Za-z]+$",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        body = b'{"message":"a real write"}'
        request = urllib.request.Request(f"http://127.0.0.1:{free_tcp_port}/api/write", data=body)
        for _attempt in range(40):
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    assert response.read() == body
                break
            except ConnectionError:
                time.sleep(0.05)
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, ConnectionRefusedError):
                    raise
                time.sleep(0.05)
        else:
            raise AssertionError("Edge proxy never became available")
        assert received == [("/api/write", f"127.0.0.1:{upstream_port}", body)]
    finally:
        process.terminate()
        process.wait(timeout=5)
        server.shutdown()
        server.server_close()
