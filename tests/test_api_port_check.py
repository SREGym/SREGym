import socket
from types import SimpleNamespace

import pytest

from sregym.conductor import conductor_api


def _listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def test_a_taken_port_fails_with_the_port_named(monkeypatch):
    with _listener() as taken:
        port = taken.getsockname()[1]
        monkeypatch.setenv("API_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("API_PORT", str(port))

        with pytest.raises(RuntimeError, match=rf"127\.0\.0\.1:{port}.*API_PORT"):
            conductor_api.check_api_port_available()


def test_a_free_port_passes_and_is_left_free(monkeypatch):
    with _listener() as probe:
        port = probe.getsockname()[1]
    monkeypatch.setenv("API_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", str(port))

    conductor_api.check_api_port_available()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as again:
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        again.bind(("127.0.0.1", port))


def test_run_benchmark_stops_before_the_conductor_when_the_port_is_taken(monkeypatch):
    import main as benchmark_main

    monkeypatch.setattr(benchmark_main.os, "environ", benchmark_main.os.environ.copy())
    monkeypatch.setattr(benchmark_main, "_configure_model_environment", lambda args: ("unused", "unused"))
    monkeypatch.setattr(benchmark_main, "set_profile", lambda profile: None)

    def conductor_reached(**kwargs):
        raise AssertionError("the conductor was configured although the API port is taken")

    monkeypatch.setattr(benchmark_main, "ConductorConfig", conductor_reached)
    args = SimpleNamespace(
        agent=None,
        internet_access="open",
        container_hardening="on",
        profile="full",
        noise=False,
        use_external_harness=False,
        stages=["mitigation"],
        baseline=None,
        propagation=None,
    )
    with _listener() as taken:
        benchmark_main.os.environ["API_BIND_HOST"] = "127.0.0.1"
        benchmark_main.os.environ["API_PORT"] = str(taken.getsockname()[1])

        with pytest.raises(RuntimeError, match="API_PORT"):
            benchmark_main._run_benchmark(args)
