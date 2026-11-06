"""Exercise the guard in isolated pytest sessions without a live cluster."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def guarded_suite(pytester):
    pytester.makeini("[pytest]\nmarkers = integration: requires explicit cluster access")
    # A broken guard must not let these regression tests touch the host cluster.
    prefix = """
import functools
import inspect
import os
import subprocess
import sys
from kubernetes import client, config
from kubernetes.config import incluster_config, kube_config
client.ApiClient.call_api = lambda *args, **kwargs: "allowed"
client.rest.RESTClientObject.request = lambda *args, **kwargs: "allowed"
for owner in (config, kube_config, incluster_config):
    for name in ("load_kube_config", "load_incluster_config", "load_kube_config_from_dict"):
        if hasattr(owner, name):
            setattr(owner, name, lambda *args, **kwargs: None)
original_popen_init = subprocess.Popen.__init__
@functools.wraps(original_popen_init)
def inert_cluster_command(self, args, *rest, **kwargs):
    if any(tool in str((args, rest, kwargs)) for tool in ("kubectl", "helm")):
        return original_popen_init(self, [sys.executable, "-c", "print('inert command')"],
                                   stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"), text=kwargs.get("text"))
    return original_popen_init(self, args, *rest, **kwargs)
subprocess.Popen.__init__ = inert_cluster_command
os.system = lambda command: 0
for name in ("spawnv", "spawnvp", "spawnve", "spawnvpe"):
    if hasattr(os, name):
        original = getattr(os, name)
        @functools.wraps(original)
        def inert_spawn(*args, _original=original, **kwargs):
            arguments = inspect.signature(_original).bind(*args, **kwargs).arguments
            if any(tool in str((arguments["file"], arguments["args"])) for tool in ("kubectl", "helm")):
                return 0
            return _original(*args, **kwargs)
        setattr(os, name, inert_spawn)
cached_api = client.ApiClient()
"""
    pytester.makeconftest(prefix + Path(__file__).with_name("conftest.py").read_text())
    return pytester


def test_caught_cluster_access_still_fails(guarded_suite):
    guarded_suite.makepyfile("""
import os
import subprocess
import sys
import pytest
from kubernetes import client, config
from conftest import cached_api

@pytest.mark.parametrize("operation", [
    lambda: client.BatchV1Api().delete_namespaced_cron_job("vpa-updater", "kube-system"),
    lambda: cached_api.call_api("/api", "GET"),
    lambda: cached_api.rest_client.request("GET", "https://example.invalid/api"),
    lambda: config.load_kube_config(),
    lambda: config.load_incluster_config(),
    lambda: config.new_client_from_config(),
    lambda: subprocess.run(["kubectl", "get", "pods"]),
    lambda: subprocess.run("kubectl get pods", shell=True),
    lambda: subprocess.run(["sh", "-c", "/usr/local/bin/kubectl get pods"]),
    lambda: subprocess.run(["helm", "list"]),
    lambda: subprocess.run(["helm", "install", "app", "chart"]),
    lambda: subprocess.run(["helm", "dependency", "update", "chart", "--future-flag"]),
    lambda: subprocess.run(["helm", "template", "app", "chart", "--validate"]),
    lambda: subprocess.run(["helm", "template", "app", "chart", "--dry-run=server"]),
    lambda: subprocess.run(["helm", "template", "app", "chart", "--post-renderer", "kubectl"]),
    lambda: subprocess.run(["helm", "template", "app", "chart", "--future-flag"]),
    lambda: subprocess.run("helm template app chart; kubectl get pods", shell=True),
    lambda: subprocess.run(["helm", "template", "app", "chart"], shell=True),
    lambda: subprocess.run(["helm", "template", "app", "chart"], executable="kubectl"),
    lambda: subprocess.Popen(["placeholder"], -1, "/usr/local/bin/kubectl"),
    lambda: subprocess.Popen(["placeholder"], -1, "/usr/local/bin/helm"),
    lambda: subprocess.Popen(["helm", "template", "app", "chart"], -1, None, None, None, None, None, True, True),
    lambda: subprocess.run([sys.executable, "-c", "pass", "--kubeconfig", "/untrusted"]),
    lambda: subprocess.run([sys.executable, "-c", "pass", "--kubeconfig=/untrusted"]),
    lambda: subprocess.run('k=--kubeconfig; python -c pass --kubeconfig=/untrusted', shell=True),
    lambda: subprocess.run("printf '%s' helm; kubectl get pods", shell=True),
    lambda: subprocess.run(["env", "kubectl", "get", "pods"]),
    lambda: os.posix_spawn(sys.executable, [sys.executable, "-c", "pass", "--kubeconfig=/untrusted"], {}),
    lambda: os.posix_spawnp(sys.executable, [sys.executable, "-c", "pass", "--kubeconfig=/untrusted"], {}),
    lambda: os.system("kubectl get pods"),
])
def test_caught(operation):
    try:
        operation()
    except Exception:
        pass
""")
    result = guarded_suite.runpytest_subprocess("-q")
    result.assert_outcomes(passed=30, errors=30)
    result.stdout.fnmatch_lines(["*Blocked real Kubernetes access*"])


def test_mocked_clients_and_unrelated_subprocesses_work(guarded_suite):
    guarded_suite.makepyfile("""
import subprocess
import sys
from unittest.mock import Mock
from kubernetes import client

def test_offline(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(client, "BatchV1Api", lambda: fake)
    client.BatchV1Api().delete_namespaced_cron_job("actor", "namespace")
    fake.delete_namespaced_cron_job.assert_called_once_with("actor", "namespace")
    subprocess.run([sys.executable, "-c", "pass"], check=True)
""")
    guarded_suite.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_cluster_tool_names_in_data_are_not_commands(guarded_suite):
    guarded_suite.makepyfile("""
import subprocess
import pytest

@pytest.mark.parametrize("command,shell", [
    (["ls", "sregym/helm"], False),
    (["printf", "%s", "helm"], False),
    ("ls sregym/helm", True),
    ("printf '%s' helm", True),
    (["sh", "-c", "ls sregym/helm"], False),
    (["grep", "kubectl", "some-file"], False),
    ("grep kubectl some-file", True),
    ("printf '%s' '$(kubectl get pods)'", True),
    (["xargs", "-n", "1", "grep", "kubectl"], False),
    (["bash", "--norc", "-c", "printf '%s' helm"], False),
])
def test_data(command, shell):
    assert subprocess.check_output(command, shell=shell, text=True).strip() == "inert command"
""")
    guarded_suite.runpytest_subprocess("-q").assert_outcomes(passed=10)


def test_indirect_cluster_commands_fail_even_if_caught(guarded_suite):
    guarded_suite.makepyfile("""
import subprocess
import pytest

@pytest.mark.parametrize("command,shell", [
    ("if true; then kubectl get pods; fi", True),
    ("printf '%s' `kubectl get pods`", True),
    ('printf "%s" "$(kubectl get pods)"', True),
    ('"$(kubectl get pods)"', True),
    ("xargs kubectl get pods", True),
    (["xargs", "-0", "-I{}", "kubectl", "get", "pod", "{}"], False),
    (["xargs", "-n", "1", "kubectl", "get", "pods"], False),
    (["bash", "--norc", "-c", "kubectl get pods"], False),
    ('bash --norc -c "kubectl get pods"', True),
    ("while false; do kubectl get pods; done", True),
    ("case x in x) kubectl get pods;; esac", True),
    ("export KUBECONFIG=~/.kube/config; k=kube; ${k}ctl get pods", True),
])
def test_blocked(command, shell):
    try:
        subprocess.run(command, shell=shell)
    except RuntimeError:
        pass
""")
    result = guarded_suite.runpytest_subprocess("-q")
    result.assert_outcomes(passed=12, errors=12)
    result.stdout.fnmatch_lines(["*Blocked real Kubernetes access*"])


def test_legacy_spawn_cluster_commands_fail_even_if_caught(guarded_suite):
    guarded_suite.makepyfile("""
import os
import pytest

@pytest.mark.parametrize("name", ["spawnv", "spawnvp", "spawnve", "spawnvpe", "spawnl", "spawnlp", "spawnle", "spawnlpe"])
def test_blocked(name):
    if not hasattr(os, name):
        pytest.skip("spawn API unavailable")
    args = (os.P_WAIT, "kubectl", ["kubectl", "get", "pods"]) if "v" in name else (os.P_WAIT, "kubectl", "kubectl", "get", "pods")
    if name.endswith("e"):
        args += ({"KUBECONFIG": "/explicit"},)
    try:
        getattr(os, name)(*args)
    except RuntimeError:
        pass
""")
    result = guarded_suite.runpytest_subprocess("-q")
    result.assert_outcomes(passed=8, errors=8)
    result.stdout.fnmatch_lines(["*Blocked real Kubernetes access: os.spawn*"])


def test_unsupported_shell_syntax_without_cluster_references_is_allowed(guarded_suite):
    guarded_suite.makepyfile("""
import subprocess

def test_local_case():
    assert subprocess.check_output("case x in x) printf safe;; esac", shell=True, text=True) == "safe"
""")
    guarded_suite.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_threads_keep_their_originating_test_permission(guarded_suite):
    guarded_suite.makepyfile("""
import os
import subprocess
import sys
import threading
import pytest
from kubernetes import client

release_unit = threading.Event()
release_integration = threading.Event()
results = []
threads = []

def worker(event, label):
    assert event.wait(10)
    try:
        client.ApiClient().call_api("/api", "GET")
    except RuntimeError:
        results.append(label)
    for env in (None, {"PATH": os.environ["PATH"]}):
        code = "import os; from pathlib import Path; assert not Path(os.environ['KUBECONFIG']).exists(); assert 'KUBERNETES_SERVICE_HOST' not in os.environ"
        subprocess.run([sys.executable, "-c", code], env=env, check=True)
    results.append(label + "-child-isolated")

def test_01_unit():
    thread = threading.Thread(target=worker, args=(release_unit, "unit"), daemon=True)
    threads.append(thread)
    thread.start()

@pytest.mark.integration
def test_02_integration():
    assert client.ApiClient().call_api("/api", "GET") == "allowed"
    allowed = []
    own_thread = threading.Thread(target=lambda: allowed.append(client.ApiClient().call_api("/api", "GET")))
    own_thread.start()
    own_thread.join(timeout=10)
    assert allowed == ["allowed"]
    release_unit.set()
    threads[0].join(timeout=10)
    assert results == ["unit", "unit-child-isolated"]
    thread = threading.Thread(target=worker, args=(release_integration, "expired"), daemon=True)
    threads.append(thread)
    thread.start()

def test_03_unit():
    release_integration.set()
    threads[1].join(timeout=10)
    assert results == ["unit", "unit-child-isolated", "expired", "expired-child-isolated"]
""")
    guarded_suite.runpytest_subprocess("-q", "--allow-kubernetes").assert_outcomes(passed=3, errors=2)


def test_real_sdk_redirects_and_streams_cannot_escape(pytester):
    pytester.makeini("[pytest]")
    # Keep the real SDK and redirect machinery; replace the final network
    # boundaries before installing the guard so a regression remains harmless.
    prefix = """
import urllib3
from kubernetes.stream import ws_client, stream, portforward
http_calls = []
websocket_calls = []
redirect_target = None
def inert_http(self, method, url, *args, **kwargs):
    http_calls.append((self.host, self.port, method, url))
    if url == "/start":
        return urllib3.response.HTTPResponse(status=307, headers={"Location": redirect_target}, body=b"")
    return urllib3.response.HTTPResponse(status=200, headers={"Content-Type": "application/json"}, body=b"{}")
def inert_websocket(*args, **kwargs):
    websocket_calls.append((args, kwargs))
    raise AssertionError("websocket boundary reached")
urllib3.connectionpool.HTTPConnectionPool.urlopen = inert_http
ws_client.create_websocket = inert_websocket
"""
    pytester.makeconftest(prefix + Path(__file__).with_name("conftest.py").read_text())
    pytester.makepyfile("""
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import Mock
import pytest
import urllib3
from kubernetes import client
import conftest as state

@pytest.mark.parametrize("target", ["same_server", "other_port", "other_host"])
@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_redirect(target, method, allow_kubernetes_test_server):
    state.http_calls.clear()
    with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        allow_kubernetes_test_server(server)
        host = f"http://127.0.0.1:{server.server_port}"
        state.redirect_target = {
            "same_server": host + "/end",
            "other_port": "http://127.0.0.1:1/end",
            "other_host": "http://unregistered.invalid/end",
        }[target]
        api = client.ApiClient(client.Configuration(host=host))
        try:
            api.call_api("/start", method)
        except RuntimeError:
            pass  # The recorded violation must still fail teardown.
        assert len(state.http_calls) == (2 if target == "same_server" else 1)
        assert all(call[:2] == ("127.0.0.1", server.server_port) for call in state.http_calls)
    # The SDK-specific context must be reset even after a blocked redirect.
    urllib3.PoolManager().request("GET", "http://unrelated.invalid/end")
    assert state.http_calls[-1][0] == "unrelated.invalid"

@pytest.mark.parametrize("operation", ["exec", "portforward"])
def test_stream_with_mocked_http_transport(operation):
    state.websocket_calls.clear()
    api = client.ApiClient(client.Configuration(host="https://never-contact.invalid"))
    api.rest_client.pool_manager.request = Mock()
    core = client.CoreV1Api(api)
    try:
        if operation == "exec":
            state.stream(core.connect_get_namespaced_pod_exec, "pod", "ns", command=["true"], _preload_content=False)
        else:
            state.portforward(core.connect_get_namespaced_pod_portforward, "pod", "ns", ports="8080")
    except Exception:
        pass
    assert not state.websocket_calls
""")
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=8, errors=6)
    result.stdout.fnmatch_lines(
        [
            "*Blocked real Kubernetes access: PoolManager.urlopen*",
            "*Blocked real Kubernetes access: kubernetes.stream.ws_client.create_websocket*",
        ]
    )


def test_offline_helm_commands_are_allowed(guarded_suite):
    guarded_suite.makepyfile("""
import subprocess
import pytest

@pytest.mark.parametrize("command", [
    ["helm", "template", "app", "chart"],
    ["/usr/local/bin/helm", "template", "app", "chart", "--namespace", "test", "-f", "values.yaml"],
    ["helm", "template", "app", "chart", "--namespace=test", "--include-crds", "--set", "image.tag=test"],
    ["helm", "dependency", "list", "chart"],
    ["helm", "dependency", "update", "chart"],
])
def test_render(command):
    assert subprocess.check_output(command, text=True).strip() == "inert command"
""")
    guarded_suite.runpytest_subprocess("-q").assert_outcomes(passed=5)


def test_mock_server_exception_is_limited_to_its_open_socket(guarded_suite):
    guarded_suite.makepyfile("""
from http.server import HTTPServer, BaseHTTPRequestHandler
from kubernetes import client
import pytest

@pytest.mark.parametrize("scenario", ["allowed", "other_port", "closed", "transport_override"])
def test_server(allow_kubernetes_test_server, scenario):
    with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        allow_kubernetes_test_server(server)
        port = server.server_port
        host = f"http://127.0.0.1:{port}"
        api = client.ApiClient(client.Configuration(host=host))
        if scenario == "other_port":
            api.configuration.host = "http://127.0.0.1:1"
        if scenario == "closed":
            server.server_close()
        try:
            assert api.call_api("/api", "GET") == "allowed"
            url = "http://127.0.0.1:1/api" if scenario == "transport_override" else host + "/api"
            assert api.rest_client.request("GET", url) == "allowed"
        except RuntimeError:
            pass
""")
    guarded_suite.runpytest_subprocess("-q").assert_outcomes(passed=4, errors=3)


def test_only_registered_temporary_configs_are_allowed(guarded_suite):
    guarded_suite.makepyfile("""
from kubernetes import config
import pytest

@pytest.mark.parametrize("scenario", ["registered", "unregistered", "default", "mixed"])
def test_config(tmp_path, allow_kubernetes_test_config, scenario):
    allowed = tmp_path / "allowed.yaml"
    unknown = tmp_path / "unknown.yaml"
    allow_kubernetes_test_config(allowed)
    with pytest.raises(ValueError):
        allow_kubernetes_test_config(tmp_path.parent / "outside.yaml")
    try:
        if scenario == "default":
            config.load_kube_config()
        else:
            path = str(allowed) if scenario == "registered" else str(unknown)
            if scenario == "mixed":
                path = f"{allowed}:{unknown}"
            config.load_kube_config(config_file=path)
    except RuntimeError:
        pass
""")
    guarded_suite.runpytest_subprocess("-q").assert_outcomes(passed=4, errors=3)


def test_only_inert_transport_mocks_bypass_the_guard(guarded_suite):
    guarded_suite.makepyfile("""
from kubernetes import client
from unittest.mock import Mock
import pytest

@pytest.mark.parametrize("scenario", ["inert", "side_effect", "wraps"])
def test_transport(scenario):
    api = client.ApiClient()
    fake = Mock()
    if scenario == "side_effect":
        fake.side_effect = lambda *args, **kwargs: None
    if scenario == "wraps":
        fake = Mock(wraps=lambda *args, **kwargs: None)
    api.rest_client.pool_manager.request = fake
    try:
        assert api.call_api("/api", "GET") == "allowed"
        assert api.rest_client.request("GET", "https://example.invalid") == "allowed"
    except RuntimeError:
        pass
""")
    guarded_suite.runpytest_subprocess("-q").assert_outcomes(passed=3, errors=2)


@pytest.mark.parametrize("opt_in", [False, True])
def test_integration_requires_marker_and_flag(guarded_suite, opt_in):
    guarded_suite.makepyfile("""
import pytest
from kubernetes import client

@pytest.mark.integration
def test_integration():
    assert client.ApiClient().call_api("/api", "GET") == "allowed"

def test_unit():
    try:
        client.ApiClient().call_api("/api", "GET")
    except RuntimeError:
        pass
""")
    args = ["-q", "--allow-kubernetes"] if opt_in else ["-q"]
    result = guarded_suite.runpytest_subprocess(*args)
    result.assert_outcomes(passed=2 if opt_in else 1, skipped=0 if opt_in else 1, errors=1)


def test_collection_is_guarded_even_with_opt_in(guarded_suite):
    guarded_suite.makepyfile("""
from kubernetes import client
try:
    client.ApiClient().call_api("/api", "GET")
except Exception:
    pass

def test_unused():
    pass
""")
    result = guarded_suite.runpytest_subprocess("-q", "--allow-kubernetes")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*Blocked Kubernetes access during collection*"])


def test_fixture_setup_and_teardown_are_guarded(guarded_suite):
    guarded_suite.makepyfile("""
import pytest
from kubernetes import client

@pytest.fixture
def cluster_cleanup():
    try:
        client.ApiClient().call_api("/setup", "GET")
    except Exception:
        pass
    yield
    try:
        client.ApiClient().call_api("/cleanup", "DELETE")
    except Exception:
        pass

def test_fixture(cluster_cleanup):
    pass
""")
    result = guarded_suite.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*ApiClient.call_api, ApiClient.call_api*"])


@pytest.mark.parametrize("opt_in", [False, True])
def test_child_process_environment_is_isolated_and_restored(pytester, opt_in):
    import json
    import sys

    pytester.makeini("[pytest]\nmarkers = integration: requires explicit cluster access")
    upstream = pytester.path / "upstream.yaml"
    upstream.write_text(
        "apiVersion: v1\nkind: Config\ncurrent-context: fake\n"
        "contexts:\n- name: fake\n  context: {cluster: fake, user: fake}\n"
        "clusters:\n- name: fake\n  cluster: {server: 'https://never-contact.invalid'}\n"
        "users:\n- name: fake\n  user: {token: fake}\n"
    )
    baseline = {
        "KUBECONFIG": str(upstream),
        "KUBERNETES_SERVICE_HOST": "never-contact.invalid",
        "KUBERNETES_SERVICE_PORT": "443",
        "KUBERNETES_SERVICE_PORT_HTTPS": "443",
        "KUBERNETES_MASTER": "https://never-contact.invalid",
    }
    prefix = f"""
import atexit
import json
import os
from pathlib import Path
baseline = {baseline!r}
os.environ.update(baseline)
def record_cleanup():
    Path("restored.json").write_text(json.dumps({{key: os.environ.get(key) for key in baseline}}))
atexit.register(record_cleanup)
"""
    pytester.makeconftest(prefix + Path(__file__).with_name("conftest.py").read_text())
    # A missing guard only causes a failed assertion: this child never makes
    # an API request, even if it succeeds in loading the fake upstream config.
    child = pytester.path / "probe.py"
    child.write_text(
        f"#!{sys.executable}\n"
        "import os\nfrom pathlib import Path\nfrom kubernetes import config\n"
        "assert not Path(os.environ['KUBECONFIG']).exists()\n"
        "assert 'KUBERNETES_SERVICE_HOST' not in os.environ\n"
        "assert 'KUBERNETES_SERVICE_PORT' not in os.environ\n"
        "for loader in (config.load_kube_config, config.load_incluster_config):\n"
        "    try:\n        loader()\n"
        "    except config.ConfigException:\n        pass\n"
        "    else:\n        raise AssertionError('loaded cluster configuration')\n"
        "print('isolated')\n"
    )
    fake_bin = pytester.path / "bin"
    fake_bin.mkdir()
    fake_kubectl = fake_bin / "kubectl"
    fake_kubectl.write_text(child.read_text())
    fake_kubectl.chmod(0o755)
    pytester.makepyfile("""
import os
from pathlib import Path
import subprocess
import sys
import pytest
from conftest import baseline

def assert_isolated():
    assert os.environ["KUBECONFIG"] != baseline["KUBECONFIG"]
    assert not Path(os.environ["KUBECONFIG"]).exists()
    assert all(key not in os.environ for key in baseline if key != "KUBECONFIG")

# Collection is isolated even when --allow-kubernetes is supplied.
assert_isolated()

def test_before(monkeypatch):
    assert_isolated()
    assert subprocess.check_output([sys.executable, "probe.py"], text=True).strip() == "isolated"
    custom = {"PATH": os.environ["PATH"], "HOME": str(Path.cwd()), "KUBERNETES_SERVICE_HOST": "never-contact.invalid"}
    saved = custom.copy()
    assert subprocess.check_output([sys.executable, "probe.py"], env=custom, text=True).strip() == "isolated"
    assert custom == saved  # Sanitization must not mutate the caller's mapping.
    monkeypatch.setenv("PATH", str(Path("bin").resolve()) + os.pathsep + os.environ["PATH"])
    assert subprocess.check_output("k=kube; ${k}ctl get pods", shell=True, text=True).strip() == "isolated"
    if hasattr(os, "posix_spawnp"):
        pid = os.posix_spawnp(sys.executable, [sys.executable, "probe.py"], os.environ.copy())
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
        pid = os.posix_spawnp(sys.executable, [sys.executable, "probe.py"], custom)
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 0
    if hasattr(os, "spawnvpe"):
        assert os.spawnvpe(os.P_WAIT, sys.executable, [sys.executable, "probe.py"], custom) == 0
        assert os.spawnlp(os.P_WAIT, sys.executable, sys.executable, "probe.py") == 0
        assert os.spawnlp(os.P_WAIT, sys.executable, sys.executable, "-c", "raise SystemExit(7)") == 7

@pytest.fixture
def integration_environment():
    assert {key: os.environ.get(key) for key in baseline} == baseline
    yield
    assert {key: os.environ.get(key) for key in baseline} == baseline

@pytest.mark.integration
def test_integration(integration_environment):
    # The integration child may load only the fake config; never call an API.
    code = "from kubernetes import client, config; config.load_kube_config(); print(client.Configuration.get_default_copy().host)"
    assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == "https://never-contact.invalid"

def test_after():
    assert_isolated()
    assert subprocess.check_output([sys.executable, "probe.py"], text=True).strip() == "isolated"
""")
    args = ["-q", "--allow-kubernetes"] if opt_in else ["-q"]
    result = pytester.runpytest_subprocess(*args)
    result.assert_outcomes(passed=3 if opt_in else 2, skipped=0 if opt_in else 1)
    assert json.loads((pytester.path / "restored.json").read_text()) == baseline


def test_unset_kubernetes_environment_is_restored(pytester):
    pytester.makeini("[pytest]\nmarkers = integration: requires explicit cluster access")
    prefix = """
import atexit
import os
from pathlib import Path
names = ("KUBECONFIG", "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT",
         "KUBERNETES_SERVICE_PORT_HTTPS", "KUBERNETES_MASTER")
for name in names:
    os.environ.pop(name, None)
atexit.register(lambda: Path("restored.txt").write_text(str(all(name not in os.environ for name in names))))
"""
    pytester.makeconftest(prefix + Path(__file__).with_name("conftest.py").read_text())
    pytester.makepyfile("""
import os
import pytest
from conftest import names

@pytest.mark.integration
def test_unset():
    assert all(name not in os.environ for name in names)

def test_isolated_again():
    assert os.environ["KUBECONFIG"]
    assert all(name not in os.environ for name in names if name != "KUBECONFIG")
""")
    pytester.runpytest_subprocess("-q", "--allow-kubernetes").assert_outcomes(passed=2)
    assert (pytester.path / "restored.txt").read_text() == "True"
