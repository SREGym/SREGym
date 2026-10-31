"""Keep pytest offline unless a cluster integration test is explicitly enabled."""

import functools
import inspect
import os
import re
import socket
import subprocess
import tempfile
import threading
from contextvars import ContextVar, copy_context
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import bashlex
import pytest
import urllib3
from kubernetes import client, config
from kubernetes.config import incluster_config, kube_config
from kubernetes.stream import ws_client

_GUARD = pytest.StashKey()
_KUBECONFIG_FLAG = re.compile(r"(?<![\w-])--kubeconfig(?:[=\s]|$)")
_KUBERNETES_ENVIRONMENT = (
    "KUBECONFIG",
    "KUBERNETES_SERVICE_HOST",
    "KUBERNETES_SERVICE_PORT",
    "KUBERNETES_SERVICE_PORT_HTTPS",
    "KUBERNETES_MASTER",
)


def _cluster_command(command, *, executable=None, shell=False):
    """Inspect executable positions, not ordinary path or text arguments."""
    tokens = [os.fsdecode(arg) for arg in command] if isinstance(command, (list, tuple)) else [os.fsdecode(command)]
    if any(_KUBECONFIG_FLAG.search(arg) for arg in tokens):
        return True
    if shell:

        class Commands(bashlex.ast.nodevisitor):
            blocked = False

            def visitcommand(self, node, parts):
                words = [part.word for part in parts if part.kind in {"word", "assignment"}]
                self.blocked |= _cluster_command(words)
                # Keep visiting: arguments may contain command substitutions.

        visitor = Commands()
        try:
            for tree in bashlex.parse(tokens[0]):
                visitor.visit(tree)
        except (ValueError, NotImplementedError, bashlex.errors.ParsingError):
            # bashlex does not implement every shell construct (e.g. case).
            # Be conservative about cluster references without rejecting
            # unrelated local scripts such as the PostgreSQL probe tests.
            return bool(re.search(r"(?<![\w.-])(?:kubectl|helm)(?![\w.-])", tokens[0])) or any(
                name in tokens[0] for name in _KUBERNETES_ENVIRONMENT
            )
        return visitor.blocked
    if not tokens:
        return False
    if executable is not None:
        tokens[0] = os.fsdecode(executable)
    while tokens and ("=" in tokens[0] and not tokens[0].startswith("-")):
        key = tokens.pop(0).split("=", 1)[0]
        if key in _KUBERNETES_ENVIRONMENT:
            return True
    if not tokens:
        return False
    program = os.path.basename(tokens[0])
    if program in {"kubectl", "helm"}:
        return True
    if program in {"sh", "bash", "zsh", "dash", "ksh"}:
        for index, token in enumerate(tokens[1:], 1):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", token) and index + 1 < len(tokens):
                return _cluster_command(tokens[index + 1], shell=True)
    if program == "xargs":
        rest = tokens[1:]
        value_flags = {
            "-I",
            "-J",
            "-n",
            "-L",
            "-P",
            "-s",
            "-E",
            "-d",
            "--max-args",
            "--max-lines",
            "--max-procs",
            "--max-chars",
            "--eof",
            "--delimiter",
        }
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if option == "--":
                break
            flag, equals, _ = option.partition("=")
            if flag in value_flags:
                if not equals:
                    if not rest:
                        return True
                    rest.pop(0)
            elif (
                option
                in {
                    "-0",
                    "-r",
                    "-t",
                    "-p",
                    "-x",
                    "--null",
                    "--no-run-if-empty",
                    "--verbose",
                    "--interactive",
                    "--exit",
                    "--replace",
                }
                or option.startswith("--replace=")
                or (len(option) > 2 and option[:2] in value_flags)
            ):
                pass
            else:
                return True
        return _cluster_command(rest) if rest else False
    if program == "eval":
        return _cluster_command(" ".join(tokens[1:]), shell=True)
    if program in {"export", "readonly"}:
        return any(arg.split("=", 1)[0] in _KUBERNETES_ENVIRONMENT for arg in tokens[1:])
    if program in {"env", "command", "exec", "sudo", "nohup"}:
        rest = tokens[1:]
        # Clearing the environment would undo child-process isolation.
        if program == "env" and any(
            arg == "-i" or arg == "--ignore-environment" or arg == "-u" or arg.startswith("--unset") for arg in rest
        ):
            return True
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return _cluster_command(rest) if rest else False
    return False


def _offline_helm_command(command):
    """Allow direct chart operations that do not use the Kubernetes API."""
    if not isinstance(command, (list, tuple)) or len(command) < 2:
        return False
    tokens = [os.fsdecode(arg) for arg in command]
    if os.path.basename(tokens[0]) != "helm":
        return False
    if tokens[1] == "dependency":
        return len(tokens) == 4 and tokens[2] in {"list", "update"} and not tokens[3].startswith("-")
    if tokens[1] != "template":
        return False
    value_flags = {"-f", "--values", "-n", "--namespace", "--kube-version", "--api-versions", "--set", "--set-string"}
    boolean_flags = {"--include-crds", "--skip-tests", "--no-hooks", "--is-upgrade", "--debug"}
    args = iter(tokens[2:])
    for arg in args:
        if not arg.startswith("-"):
            continue
        flag, separator, value = arg.partition("=")
        if flag in value_flags:
            if not separator and next(args, None) is None:
                return False
        elif flag in boolean_flags:
            if separator and value not in {"true", "false"}:
                return False
        else:
            # In particular: --validate, --dry-run=server and --post-renderer
            # can contact a cluster or execute arbitrary code.
            return False
    return True


def pytest_addoption(parser):
    parser.addoption(
        "--allow-kubernetes",
        action="store_true",
        help="Allow real Kubernetes access only in tests marked integration.",
    )


def pytest_configure(config):
    guard = _KubernetesGuard()
    config.stash[_GUARD] = guard
    config.add_cleanup(guard.close)
    guard.install()


class _KubernetesGuard:
    def __init__(self):
        self.patches = pytest.MonkeyPatch()
        self.environment = pytest.MonkeyPatch()
        self.original_environment = {name: os.environ.get(name) for name in _KUBERNETES_ENVIRONMENT}
        self.environment_dir = tempfile.TemporaryDirectory(prefix="sregym-pytest-")
        self.disabled_kubeconfig = str(Path(self.environment_dir.name) / "missing-kubeconfig")
        self.access_context = ContextVar("kubernetes_test_access", default=None)
        self.current_access = None
        self.attempts = []
        self.mock_servers = []
        self.test_config_paths = set()
        self.sdk_http_request = ContextVar("kubernetes_test_http_request", default=False)

    def set_live_access(self, allowed):
        if self.current_access is not None:
            self.current_access["active"] = False
        self.current_access = {"live": allowed, "active": True}
        self.access_context.set(self.current_access)
        # Child processes do not inherit Python patches, but do inherit these
        # defaults. Keep a nonempty KUBECONFIG to avoid ~/.kube/config fallback.
        for name, original in self.original_environment.items():
            value = original if allowed else (self.disabled_kubeconfig if name == "KUBECONFIG" else None)
            if value is None:
                self.environment.delenv(name, raising=False)
            elif os.environ.get(name) != value:
                self.environment.setenv(name, value)

    @property
    def allowed(self):
        access = self.access_context.get()
        return bool(access and access["live"] and access["active"])

    def child_environment(self, environment):
        result = dict(os.environ if environment is None else environment)
        for name in _KUBERNETES_ENVIRONMENT:
            result.pop(name, None)
            result.pop(os.fsencode(name), None)
        result["KUBECONFIG"] = self.disabled_kubeconfig
        return result

    def protect_threads(self):
        original = threading.Thread.start

        @functools.wraps(original)
        def start(thread, *args, **kwargs):
            context = copy_context()
            run = thread.run
            thread.run = lambda: context.run(run)
            return original(thread, *args, **kwargs)

        self.patches.setattr(threading.Thread, "start", start)

    def protect_spawn(self, name):
        original = getattr(os, name)

        @functools.wraps(original)
        def spawn(path, argv, env, **kwargs):
            if not self.allowed:
                if _cluster_command(argv, executable=path) and not _offline_helm_command([path, *argv[1:]]):
                    self.block(f"os.{name}")
                env = self.child_environment(env)
            return original(path, argv, env, **kwargs)

        self.patches.setattr(os, name, spawn)

    def protect_legacy_spawn(self, name, spawn_with_environment):
        original = getattr(os, name)
        signature = inspect.signature(original)

        @functools.wraps(original)
        def spawn(*args, **kwargs):
            if self.allowed:
                return original(*args, **kwargs)
            arguments = signature.bind(*args, **kwargs).arguments
            path, argv = arguments["file"], arguments["args"]
            if _cluster_command(argv, executable=path) and not _offline_helm_command([path, *argv[1:]]):
                self.block(f"os.{name}")
            return spawn_with_environment(arguments["mode"], path, argv, self.child_environment(arguments.get("env")))

        self.patches.setattr(os, name, spawn)

    def close(self):
        try:
            self.patches.undo()
        finally:
            self.environment.undo()
            self.environment_dir.cleanup()

    @staticmethod
    def mocked_transport(rest):
        request = rest.pool_manager.request
        return isinstance(request, Mock) and request.side_effect is None and request._mock_wraps is None

    def permits_mock_server(self, url):
        endpoint = urlsplit(url)
        return (
            endpoint.scheme == "http"
            and endpoint.hostname == "127.0.0.1"
            and any(
                sock.fileno() >= 0 and sock.getsockname() == ("127.0.0.1", endpoint.port) for sock in self.mock_servers
            )
        )

    def block(self, operation):
        self.attempts.append(operation)
        raise RuntimeError(
            f"Blocked real Kubernetes access: {operation}. Mock the cluster dependency; "
            "live tests require @pytest.mark.integration and --allow-kubernetes."
        )

    def protect(self, owner, name, *, command=False):
        original = getattr(owner, name)
        signature = inspect.signature(original) if command else None

        @functools.wraps(original)
        def guarded(*args, **kwargs):
            if not self.allowed:
                should_block = True
                if owner is client.ApiClient and name == "call_api":
                    should_block = not (
                        self.mocked_transport(args[0].rest_client)
                        or self.permits_mock_server(args[0].configuration.host)
                    )
                elif owner is client.rest.RESTClientObject and name == "request":
                    url = kwargs.get("url", args[2] if len(args) > 2 else "")
                    should_block = not (self.mocked_transport(args[0]) or self.permits_mock_server(url))
                elif owner is urllib3.PoolManager and name == "urlopen":
                    # Redirects recurse here below RESTClientObject.request.
                    url = kwargs.get("url", args[2] if len(args) > 2 else "")
                    should_block = self.sdk_http_request.get() and not self.permits_mock_server(url)
                elif name == "load_kube_config":
                    filename = kwargs.get("config_file", args[0] if args else None)
                    if filename:
                        paths = os.fsdecode(filename).split(os.pathsep)
                        should_block = not all(str(Path(path).resolve()) in self.test_config_paths for path in paths)
                if command:
                    bound = signature.bind(*args, **kwargs)
                    arguments = bound.arguments
                    value = arguments.get("args", arguments.get("command", ""))
                    executable = arguments.get("executable")
                    should_block = _cluster_command(
                        value, executable=executable, shell=name == "system" or arguments.get("shell", False)
                    )
                    if (
                        name == "__init__"
                        and not arguments.get("shell", False)
                        and executable is None
                        and _offline_helm_command(value)
                    ):
                        should_block = False
                    if name == "__init__":
                        bound.arguments["env"] = self.child_environment(arguments.get("env"))
                        args, kwargs = bound.args, bound.kwargs
                if should_block:
                    self.block(f"{owner.__name__}.{name}")
                if command and name == "system":
                    code = subprocess.call(value, shell=True, env=self.child_environment(None))
                    return code << 8 if code >= 0 else -code
            if owner is client.rest.RESTClientObject and name == "request":
                token = self.sdk_http_request.set(True)
                try:
                    return original(*args, **kwargs)
                finally:
                    self.sdk_http_request.reset(token)
            return original(*args, **kwargs)

        self.patches.setattr(owner, name, guarded)

    def install(self):
        self.set_live_access(False)
        self.protect_threads()
        # Guard before collection, including clients using already-loaded config.
        self.protect(client.ApiClient, "call_api")
        self.protect(client.rest.RESTClientObject, "request")
        self.protect(urllib3.PoolManager, "urlopen")
        # stream/portforward capture their callables in partials at import time,
        # but both resolve this connection function when they execute.
        self.protect(ws_client, "create_websocket")
        for name in ("load_kube_config", "load_incluster_config", "load_kube_config_from_dict"):
            self.protect(config, name)
        # Factory helpers resolve loaders in these modules, not the public aliases.
        for name in ("load_kube_config", "load_kube_config_from_dict"):
            self.protect(kube_config, name)
        self.protect(incluster_config, "load_incluster_config")
        # run/check_call/check_output and asyncio subprocesses use Popen.
        self.protect(subprocess.Popen, "__init__", command=True)
        self.protect(os, "system", command=True)
        for name in ("posix_spawn", "posix_spawnp"):
            if hasattr(os, name):
                self.protect_spawn(name)
        # spawnl/spawnlp/spawnle/spawnlpe delegate to these vector forms.
        legacy = {name: getattr(os, name) for name in ("spawnve", "spawnvpe") if hasattr(os, name)}
        for name, target in (
            ("spawnv", "spawnve"),
            ("spawnve", "spawnve"),
            ("spawnvp", "spawnvpe"),
            ("spawnvpe", "spawnvpe"),
        ):
            if hasattr(os, name) and target in legacy:
                self.protect_legacy_spawn(name, legacy[target])


@pytest.fixture
def allow_kubernetes_test_server(request):
    """Permit an SDK test's own bound HTTP server, never all loopback traffic."""
    guard = request.config.stash[_GUARD]
    registered = []

    def allow(server):
        sock = server.socket if isinstance(server, HTTPServer) else server
        if not isinstance(sock, socket.socket) or sock.getsockname()[0] != "127.0.0.1":
            raise ValueError("Expected a test-owned server or socket bound to 127.0.0.1")
        guard.mock_servers.append(sock)
        registered.append(sock)

    yield allow
    for server in registered:
        guard.mock_servers.remove(server)


@pytest.fixture
def allow_kubernetes_test_config(request, tmp_path):
    """Allow only explicitly registered kubeconfigs under this test's tmp_path."""
    guard = request.config.stash[_GUARD]
    registered = set()

    def allow(*paths):
        for path in paths:
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(tmp_path.resolve()):
                raise ValueError("Test kubeconfigs must be under tmp_path")
            registered.add(str(resolved))
            guard.test_config_paths.add(str(resolved))

    yield allow
    guard.test_config_paths.difference_update(registered)


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--allow-kubernetes"):
        skip = pytest.mark.skip(reason="integration tests require --allow-kubernetes")
        for item in items:
            if item.get_closest_marker("integration"):
                item.add_marker(skip)
    guard = config.stash[_GUARD]
    if guard.attempts:
        raise pytest.UsageError("Blocked Kubernetes access during collection: " + ", ".join(guard.attempts))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    guard = item.config.stash[_GUARD]
    guard.set_live_access(bool(item.config.getoption("--allow-kubernetes") and item.get_closest_marker("integration")))
    guard.attempts.clear()
    try:
        yield
    finally:
        guard.set_live_access(False)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    guard = item.config.stash[_GUARD]
    if call.when == "teardown" and guard.attempts:
        # Application cleanup often catches Exception. Do not let that hide a violation.
        report = outcome.get_result()
        report.outcome = "failed"
        report.longrepr = "Blocked real Kubernetes access: " + ", ".join(guard.attempts)
