"""Existing table sizes drive traffic; no injection may mutate host limits."""

import ast
import errno
import os
import select
import socket as real_socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from sregym.conductor.problems import node_conntrack_exhaustion as module

if os.name == "posix":
    import resource


@pytest.mark.parametrize("maximum", [65536, 262144, 524288, 1048576])
def test_calibration_targets_the_existing_limit_without_any_write(monkeypatch, maximum):
    problem = object.__new__(module.NodeConntrackExhaustionHotelReservation)
    problem.victim_node, problem.namespace = "worker", "application"
    problem.kubectl = Mock()
    read = Mock(return_value=(100, maximum))
    monkeypatch.setattr(module, "read_node_conntrack_usage", read)
    problem._prepare_conntrack_limit()
    assert problem.target_connections == (maximum * 105 + 99) // 100
    assert problem.original_conntrack_max == maximum
    assert problem.gateway_port_count * 20000 >= problem.target_connections
    assert problem.kubectl.mock_calls == []
    read.assert_called_once_with(problem.kubectl, "worker", "application")


@pytest.mark.parametrize(
    "measurements", [(0, 0), (-1, 262144), (100, -1), (262144, 262144), (100, 4194304), (30000, 262144)]
)
def test_invalid_saturated_or_unaffordable_tables_fail_before_creating_traffic(monkeypatch, measurements):
    problem = object.__new__(module.NodeConntrackExhaustionHotelReservation)
    problem.victim_node, problem.namespace = "worker", "application"
    problem.kubectl = Mock()
    monkeypatch.setattr(module, "read_node_conntrack_usage", Mock(return_value=measurements))
    with pytest.raises(RuntimeError):
        problem._prepare_conntrack_limit()
    assert not hasattr(problem, "target_connections")
    assert problem.kubectl.mock_calls == []


@pytest.fixture
def problem():
    instance = object.__new__(module.NodeConntrackExhaustionHotelReservation)
    instance.victim_node, instance.namespace = "worker", "application"
    instance.kubectl, instance.apps_v1, instance.core_v1 = Mock(), Mock(), Mock()
    instance.fault_injected = True
    instance.target_connections = 100
    return instance


@pytest.mark.parametrize("failure", ["no_node", "selection_error", "invalid_sample", "drain_error", "deletion_error"])
def test_unsuccessful_recovery_preserves_fault_state(problem, monkeypatch, failure):
    problem._delete_support_resources = Mock()
    problem.select_worker_nodes = Mock(return_value=(None, None))
    if failure in {"no_node", "selection_error"}:
        problem.victim_node = None
    if failure == "selection_error":
        problem.select_worker_nodes.side_effect = RuntimeError("no verified victim")
    if failure == "deletion_error":
        problem._delete_support_resources.side_effect = RuntimeError("delete denied")
    monkeypatch.setattr(module, "read_node_conntrack_usage", Mock(return_value=(0, 0)))
    if failure == "drain_error":
        problem._wait_for_conntrack = Mock(side_effect=RuntimeError("still saturated"))
    with pytest.raises(RuntimeError):
        problem.recover_fault()
    assert problem.fault_injected is True


def test_verified_recovery_clears_fault_state(problem, monkeypatch):
    problem._delete_support_resources = Mock()
    monkeypatch.setattr(module, "read_node_conntrack_usage", Mock(return_value=(10, 1000)))
    problem.recover_fault()
    assert problem.fault_injected is False


@pytest.mark.parametrize("measurements", [(0, 0), (-1, 100), (1, -1)])
@pytest.mark.parametrize("below", [True, False])
def test_invalid_samples_never_establish_recovery_or_saturation(problem, monkeypatch, measurements, below):
    monkeypatch.setattr(module, "read_node_conntrack_usage", Mock(return_value=measurements))
    with pytest.raises(RuntimeError, match="Invalid conntrack"):
        problem._wait_for_conntrack("worker", 0.1, timeout=1, below=below)


def test_cleanup_attempts_all_support_resources_and_exposes_failures(problem):
    problem.apps_v1.delete_namespaced_deployment.side_effect = [ApiException(status=403), ApiException(status=500)]
    problem.core_v1.delete_namespaced_service.side_effect = ApiException(status=503)
    with pytest.raises(ExceptionGroup) as raised:
        problem._delete_support_resources()
    assert len(raised.value.exceptions) == 3
    assert problem.apps_v1.delete_namespaced_deployment.call_count == 2
    problem.core_v1.delete_namespaced_service.assert_called_once()


def test_repeated_missing_resource_cleanup_is_idempotent(problem):
    problem.apps_v1.delete_namespaced_deployment.side_effect = ApiException(status=404)
    problem.core_v1.delete_namespaced_service.side_effect = ApiException(status=404)
    problem._delete_support_resources()
    problem._delete_support_resources()
    assert problem.apps_v1.delete_namespaced_deployment.call_count == 4


@pytest.mark.parametrize("cleanup_error", [None, "delete", "drain"])
def test_failed_partial_injection_cleans_and_verifies_drain(problem, cleanup_error):
    problem.fault_injected = False
    problem.select_worker_nodes = Mock(return_value=("worker", "worker2"))
    problem._prepare_conntrack_limit = Mock()
    problem._delete_support_resources = Mock(
        side_effect=[None, RuntimeError("cleanup failed") if cleanup_error == "delete" else None]
    )
    problem._wait_for_conntrack = Mock(side_effect=RuntimeError("drain failed") if cleanup_error == "drain" else None)
    problem.core_v1.create_namespaced_service.side_effect = RuntimeError("create failed")
    with pytest.raises(RuntimeError, match="cleanup could not" if cleanup_error else "create failed"):
        problem.inject_fault()
    assert problem._delete_support_resources.call_count == 2
    assert problem.fault_injected is (cleanup_error is not None)
    if cleanup_error != "delete":
        problem._wait_for_conntrack.assert_called_once_with("worker", 0.1, timeout=180, below=True)


def client_script(problem):
    return next(item["value"] for item in problem._client_container()["env"] if item["name"] == "SCRIPT")


@pytest.mark.parametrize("hard", [32, 65536])
def test_gateway_descriptor_capacity_is_verified_before_listening(problem, hard):
    container = problem._gateway_container()
    environment = {item["name"]: item["value"] for item in container["env"]}
    tree = ast.parse(environment["SCRIPT"])
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "listeners" for target in node.targets
        ):
            break
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            nodes.append(node)
    resource = SimpleNamespace(
        RLIMIT_NOFILE=7, RLIM_INFINITY=-1, getrlimit=Mock(return_value=(64, hard)), setrlimit=Mock()
    )
    namespace = {"os": SimpleNamespace(environ=environment), "resource": resource}
    compiled = compile(ast.Module(body=nodes, type_ignores=[]), "<gateway capacity>", "exec")
    if hard == 32:
        with pytest.raises(RuntimeError, match="capacity is insufficient"):
            exec(compiled, namespace)
        resource.setrlimit.assert_not_called()
    else:
        exec(compiled, namespace)
        assert resource.setrlimit.call_args.args[1][0] >= int(environment["REQUIRED_FDS"])


@pytest.mark.parametrize("violation", [None, "fds", "narrow_ports", "reserved_ports"])
def test_client_preflights_real_capacity_before_workers_and_resolves_ipv4_once(problem, violation):
    tree = ast.parse(client_script(problem))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            break
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            nodes.append(node)
    resource = SimpleNamespace(
        RLIMIT_NOFILE=7,
        RLIM_INFINITY=-1,
        getrlimit=Mock(return_value=(64, 32 if violation == "fds" else 65536)),
        setrlimit=Mock(),
    )
    socket = SimpleNamespace(
        AF_INET=2, SOCK_STREAM=1, getaddrinfo=Mock(return_value=[(2, 1, 6, "", ("192.0.2.4", 9090))])
    )

    def read(path):
        if path.endswith("ip_local_port_range"):
            return "40000 40099" if violation == "narrow_ports" else "32768 60999"
        return "32768-60999" if violation == "reserved_ports" else ""

    namespace = {
        "resource": resource,
        "socket": socket,
        "os": SimpleNamespace(
            environ={
                "TARGET_HOST": "gateway",
                "TARGET_PORTS": "9090,9091",
                "CONNECTIONS": "100",
                "CONNECTIONS_PER_WORKER": "10000",
            }
        ),
        "Path": lambda path: SimpleNamespace(read_text=lambda: read(path)),
    }
    compiled = compile(ast.Module(body=nodes, type_ignores=[]), "<client capacity>", "exec")
    if violation:
        with pytest.raises(RuntimeError, match="capacity is insufficient"):
            exec(compiled, namespace)
        socket.getaddrinfo.assert_not_called()
    else:
        exec(compiled, namespace)
        socket.getaddrinfo.assert_called_once_with("gateway", 9090, family=2, type=1)
        assert namespace["addrs"] == [("192.0.2.4", 9090), ("192.0.2.4", 9091)]


@pytest.mark.parametrize(
    "status,writable,error,accepted",
    [
        (0, True, 0, True),
        (errno.EINPROGRESS, True, 0, True),
        (errno.EADDRNOTAVAIL, False, 0, False),
        (errno.EINPROGRESS, True, errno.ECONNREFUSED, False),
        (errno.EINPROGRESS, False, 0, False),
    ],
)
def test_client_counts_only_completed_connections(problem, status, writable, error, accepted):
    node = next(node for node in ast.parse(client_script(problem)).body if isinstance(node, ast.FunctionDef))
    connection = Mock()
    connection.connect_ex.return_value = status
    connection.getsockopt.return_value = error

    class StopLoop(Exception):
        pass

    def sleep(interval):
        if interval == (30 if accepted else 0.02):
            raise StopLoop

    socket = SimpleNamespace(socket=Mock(return_value=connection), SOL_SOCKET=1, SO_ERROR=4)
    namespace = {
        "errno": errno,
        "socket": socket,
        "select": SimpleNamespace(
            poll=Mock(
                return_value=SimpleNamespace(register=Mock(), poll=Mock(return_value=[(4096, 4)] if writable else []))
            ),
            POLLOUT=4,
            POLLERR=8,
            POLLHUP=16,
            POLLNVAL=32,
        ),
        "time": SimpleNamespace(sleep=sleep),
        "addrs": [("192.0.2.4", 9090)],
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<client connection>", "exec"), namespace)
    with pytest.raises(StopLoop):
        namespace["hold_connections"](1)
    assert connection.close.call_count == (0 if accepted else 1)


@pytest.mark.skipif(os.name != "posix", reason="Linux poll and descriptor capacity")
def test_client_completion_supports_actual_descriptor_above_select_limit(problem):
    original_limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    if original_limits[1] != resource.RLIM_INFINITY and original_limits[1] < 1200:
        pytest.skip("Existing process hard descriptor ceiling is insufficient")
    descriptors, connections = [], []
    listener = None

    class StopLoop(Exception):
        pass

    def socket_factory():
        connection = real_socket.socket()
        connections.append(connection)
        return connection

    def sleep(interval):
        if interval == 30:
            raise StopLoop
        if interval == 0.02:
            raise AssertionError("The real high-descriptor connection failed")

    try:
        if original_limits[0] != resource.RLIM_INFINITY and original_limits[0] < 1200:
            resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (
                    min(2048, original_limits[1]) if original_limits[1] != resource.RLIM_INFINITY else 2048,
                    original_limits[1],
                ),
            )
        for _ in range(1050):
            descriptors.append(os.open("/dev/null", os.O_RDONLY))
        listener = real_socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        node = next(node for node in ast.parse(client_script(problem)).body if isinstance(node, ast.FunctionDef))
        namespace = {
            "errno": errno,
            "select": select,
            "socket": SimpleNamespace(
                socket=socket_factory, SOL_SOCKET=real_socket.SOL_SOCKET, SO_ERROR=real_socket.SO_ERROR
            ),
            "time": SimpleNamespace(sleep=sleep),
            "addrs": [listener.getsockname()],
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<high fd connection>", "exec"), namespace)
        with pytest.raises(StopLoop):
            namespace["hold_connections"](1)
        assert len(connections) == 1 and connections[0].fileno() >= 1024
        accepted, _ = listener.accept()
        accepted.close()
    finally:
        for connection in connections:
            connection.close()
        if listener is not None:
            listener.close()
        for descriptor in descriptors:
            os.close(descriptor)
        resource.setrlimit(resource.RLIMIT_NOFILE, original_limits)
