"""Check native executables and Blueprint's benchmark controls in real images.

Uses disposable containers and credential-free fixture servers, never a real
cluster. Run with the project's Python environment (grpcio is required for the
frontend/search tests). ARM must run natively for native-execution evidence.
"""

import argparse
import collections
import concurrent.futures
import csv
import http.client
import json
import platform
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVICES = ("frontend", "geo", "profile", "rate", "recomd", "reserv", "search", "user", "workload")
IS_MAC = platform.system() == "Darwin"
LISTEN_HOST = "127.0.0.1" if IS_MAC else "0.0.0.0"
HOST_ARGS = [] if IS_MAC else ["--add-host=host.docker.internal:host-gateway"]


def docker(*args, check=True, timeout=60):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=timeout)


@contextmanager
def container():
    name = "sregym-blueprint-smoke-" + uuid.uuid4().hex[:10]
    try:
        yield name
    except Exception:
        print(docker("logs", name, check=False).stderr)
        raise
    finally:
        docker("rm", "-f", name, check=False)


def check_elf(header, arch):
    assert header[:6] == b"\x7fELF\x02\x01", "Expected a little-endian ELF64 program"
    machine = int.from_bytes(header[18:20], "little")
    assert machine == {"amd64": 62, "arm64": 183}[arch], (arch, machine)


def check_program(image, arch, service):
    program = "/app/wlgen_proc" if service == "workload" else f"/{service}_service_process/{service}_service_process"
    with container() as name, tempfile.TemporaryDirectory(prefix="sregym-blueprint-elf-") as directory:
        docker("create", "--name", name, "--platform", "linux/" + arch, "--entrypoint", program, image)
        binary = Path(directory) / "program"
        docker("cp", f"{name}:{program}", str(binary))
        with binary.open("rb") as source:
            check_elf(source.read(20), arch)
    help_text = docker(
        "run", "--rm", "--platform", "linux/" + arch, "--network", "none", "--entrypoint", program, image, "--help"
    )
    assert "-jaeger.dial_addr" in help_text.stdout + help_text.stderr


def wait_http(address):
    for _ in range(100):
        try:
            urllib.request.urlopen("http://" + address, timeout=0.2).close()
            return
        except urllib.error.HTTPError:
            return  # A 404 proves the HTTP listener is ready without calling a backend.
        except (urllib.error.URLError, http.client.RemoteDisconnected, TimeoutError):
            time.sleep(0.1)
    raise AssertionError("Frontend did not become ready")


def check_retries(image, arch, service):
    import grpc

    def invoke(channel, address):
        if channel is not None:
            try:
                channel.unary_unary("/grpc.SearchService_OTServerWrapperInterface/Nearby")(b"", timeout=5)
                return True
            except grpc.RpcError:
                return False
        url = (
            "http://" + address + "/SearchHandler?lat=37.77&lon=-122.41&inDate=2015-04-09&outDate=2015-04-10&locale=en"
        )
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status == 200
        except urllib.error.HTTPError:
            return False

    observed = []
    lock = threading.Lock()

    class Backend(grpc.GenericRpcHandler):
        def service(self, details):
            def call(request, context):
                with lock:
                    observed.append((details.method, context.time_remaining()))
                    count = len(observed)
                if mode == "timeout":
                    time.sleep(0.15)
                elif mode == "error" or (mode == "recover" and count < 3):
                    context.abort(grpc.StatusCode.UNAVAILABLE, "fixture")
                return b""  # Valid protobuf response with empty result fields.

            return grpc.unary_unary_rpc_method_handler(call)

    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=32))
    server.add_generic_rpc_handlers((Backend(),))
    port = server.add_insecure_port(LISTEN_HOST + ":0")
    server.start()
    try:
        cases = [
            (None, "1s", "error"),
            (3, "1s", "error"),
            (30, "50ms", "error"),
            (3, "50ms", "timeout"),
            (5, "1s", "recover"),
        ]
        for attempts, timeout, mode in cases:
            with container() as name:
                args = [
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--platform",
                    "linux/" + arch,
                    *HOST_ARGS,
                    "--memory",
                    "256m",
                    "-p",
                    "127.0.0.1::12345",
                ]
                dependencies = (
                    ("SEARCH", "PROFILE", "RECOMD", "USER", "RESERV") if service == "frontend" else ("GEO", "RATE")
                )
                for dependency in dependencies:
                    args += ["-e", f"{dependency}_SERVICE_GRPC_DIAL_ADDR=host.docker.internal:{port}"]
                bind = "FRONTEND_SERVICE_HTTP_BIND_ADDR" if service == "frontend" else "SEARCH_SERVICE_GRPC_BIND_ADDR"
                args += [
                    "-e",
                    bind + "=0.0.0.0:12345",
                    "-e",
                    "JAEGER_DIAL_ADDR=127.0.0.1:1",
                    "-e",
                    "GRPC_CLIENT_TIMEOUT=" + timeout,
                ]
                if attempts is not None:
                    args += ["-e", "GRPC_CLIENT_RETRIES_ON_ERROR=" + str(attempts)]
                docker(*args, image)
                address = docker("port", name, "12345/tcp").stdout.strip()
                channel = grpc.insecure_channel(address) if service == "search" else None
                try:
                    if channel is None:
                        wait_http(address)
                    else:
                        grpc.channel_ready_future(channel).result(timeout=15)
                    # Establish outgoing HTTP/2 connections before measuring
                    # 50ms deadlines, especially under AMD64 emulation. This
                    # is a successful request, separate from the fault cases.
                    case_mode, mode = mode, "warmup"
                    assert invoke(channel, address), "Warmup request failed"
                    mode = case_mode
                    with lock:
                        observed.clear()
                    start = time.monotonic()
                    success = invoke(channel, address)
                    elapsed = time.monotonic() - start
                    expected = (5 if service == "frontend" else 4) if mode == "recover" else (attempts or 1)
                    assert len(observed) == expected, (expected, observed)
                    assert success == (mode == "recover"), (mode, success)
                    if mode == "timeout":
                        assert elapsed >= 0.12, elapsed
                        assert all(0 < remaining <= 0.06 for _, remaining in observed), observed
                    print(
                        json.dumps(
                            {
                                "check": "retry",
                                "service": service,
                                "mode": mode,
                                "attempts": attempts,
                                "calls": len(observed),
                                "elapsed": elapsed,
                            }
                        )
                    )
                finally:
                    if channel is not None:
                        channel.close()
    finally:
        server.stop(0).wait()


def check_workload_rows(rows, latency_rows):
    assert 850 <= len(rows) <= 1150, len(rows)
    assert all(row["IsError"] == "false" for row in rows)
    start = min(int(row["Start"]) for row in rows)
    phases = collections.Counter(min(2, int((int(row["Start"]) - start) / 2_000_000_000)) for row in rows)
    for phase, expected in enumerate((200, 600, 200)):
        assert expected * 0.8 <= phases[phase] <= expected * 1.2, phases
    # Legacy latency buckets use wall-clock seconds relative to the first
    # completed request, not the scheduler's monotonic start time.
    first_second = int(rows[0]["Start"]) // 1_000_000_000
    durations = collections.defaultdict(list)
    for row in rows:
        durations[int(row["Start"]) // 1_000_000_000 - first_second].append(int(row["Duration"]))
    assert len(latency_rows) == 6, latency_rows
    for second, average, load in latency_rows:
        second = int(second)
        assert int(load) == (300 if 2 <= second < 4 else 100), (second, load)
        expected = sum(durations[second]) / len(durations[second])
        assert abs(float(average) - expected) < 0.011, (average, expected)
    return dict(phases)


def check_workload(image, arch):
    class Frontend(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer((LISTEN_HOST, 0), Frontend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with container() as name, tempfile.TemporaryDirectory(prefix="sregym-blueprint-load-") as directory:
            result = docker(
                "run",
                "--name",
                name,
                "--platform",
                "linux/" + arch,
                *HOST_ARGS,
                "--memory",
                "256m",
                "-v",
                directory + ":/results",
                "-w",
                "/results",
                "-e",
                f"FRONTEND_SERVICE_HTTP_DIAL_ADDR=host.docker.internal:{server.server_port}",
                "-e",
                f"JAEGER_DIAL_ADDR=host.docker.internal:{server.server_port}",
                "--entrypoint",
                "/app/wlgen_proc",
                image,
                "--duration",
                "6s",
                "--tput",
                "100",
                "--multiplier",
                "3",
                "--stabletime",
                "2",
                "--triggertime",
                "2",
                "--reverttime",
                "2",
                timeout=40,
            )
            assert "Workload starting at: " in result.stdout
            assert "End of latency distribution" in result.stdout
            assert "Finished all requests" in result.stderr
            with (Path(directory) / "stats.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            with (Path(directory) / "second_latency.csv").open() as stream:
                reader = csv.reader(stream)
                assert next(reader) == ["Second", "AverageLatency(ns)"]
                latency_rows = list(reader)
            phases = check_workload_rows(rows, latency_rows)
            print(json.dumps({"check": "workload", "requests": len(rows), "phases": phases}))
    finally:
        server.shutdown()
        server.server_close()


def main(image, arch, service):
    check_program(image, arch, service)
    if service in ("frontend", "search"):
        check_retries(image, arch, service)
    elif service == "workload":
        check_workload(image, arch)
    print(json.dumps({"image": image, "arch": arch, "service": service, "passed": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("arch", choices=("amd64", "arm64"))
    parser.add_argument("service", choices=SERVICES)
    args = parser.parse_args()
    main(args.image, args.arch, args.service)
