"""Exercise an owned cluster's collector with real Jaeger and OTLP traffic.

Set SPANMETRICS_INTEGRATION=1, OTLP_ENDPOINT, PROMETHEUS_URL and JAEGER_URL.
The caller supplies port-forwards and an idle cluster; this test owns only its
two temporary namespaces. No application or collector configuration is changed.
"""

import base64
import json
import os
import subprocess
import time
import uuid

import pytest
import requests

pytestmark = pytest.mark.integration


def kubectl(*args, manifest=None):
    return subprocess.check_output(
        ["kubectl", *args], input=json.dumps(manifest) if manifest else None, text=True, timeout=180
    )


@pytest.fixture
def endpoints():
    names = ["OTLP_ENDPOINT", "PROMETHEUS_URL", "JAEGER_URL"]
    if os.environ.get("SPANMETRICS_INTEGRATION") != "1" or not all(os.environ.get(name) for name in names):
        pytest.skip("requires an owned collector and explicit spanmetrics integration endpoints")
    return tuple(os.environ[name].rstrip("/") for name in names)


@pytest.fixture
def pods(endpoints):
    prefix = f"trace-label-test-{uuid.uuid4().hex[:8]}"
    created, result = [], []
    try:
        for suffix in ("a", "b"):
            namespace = f"{prefix}-{suffix}"
            # Create, not apply: never adopt or delete another test's namespace.
            kubectl("create", "namespace", namespace)
            created.append(namespace)
            kubectl(
                "create",
                "-f",
                "-",
                manifest={
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": "frontend", "namespace": namespace},
                    "spec": {
                        "restartPolicy": "Never",
                        "terminationGracePeriodSeconds": 1,
                        "containers": [
                            {"name": "frontend", "image": "python:3.12-slim-bookworm", "command": ["sleep", "600"]}
                        ],
                    },
                },
            )
            kubectl("-n", namespace, "wait", "--for=condition=Ready", "pod/frontend", "--timeout=140s")
            result.append(json.loads(kubectl("-n", namespace, "get", "pod/frontend", "-o", "json")))
        yield result
    finally:
        for namespace in created:
            kubectl("delete", "namespace", namespace, "--wait=true", "--timeout=120s")


def send_jaeger(pod, operation, protocol, pid=None):
    agent = pytest.importorskip("opentelemetry.exporter.jaeger.thrift.gen.agent.Agent")
    types = pytest.importorskip("opentelemetry.exporter.jaeger.thrift.gen.jaeger.ttypes")
    thrift_protocol = pytest.importorskip(f"thrift.protocol.T{'Compact' if protocol == 'udp' else 'Binary'}Protocol")
    transport = pytest.importorskip("thrift.transport.TTransport")
    identity = int.from_bytes(uuid.uuid4().bytes[:7], "big")
    tags = [types.Tag(key="ip", vType=types.TagType.STRING, vStr=pod["status"]["podIP"])] if protocol == "udp" else []
    if pid is not None:
        tags.append(types.Tag(key="process.pid", vType=types.TagType.LONG, vLong=pid))
    batch = types.Batch(
        process=types.Process(serviceName="frontend", tags=tags),
        spans=[
            types.Span(
                traceIdLow=identity,
                traceIdHigh=0,
                spanId=identity,
                parentSpanId=0,
                operationName=operation,
                flags=1,
                startTime=int(time.time() * 1_000_000),
                duration=1000,
                tags=[],
            )
        ],
    )
    buffer = transport.TMemoryBuffer()
    if protocol == "udp":
        agent.Client(thrift_protocol.TCompactProtocol(buffer)).emitBatch(batch)
        source = "import base64,socket; socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(base64.b64decode(%r),('otel-collector.observe',6831))"
    else:
        batch.write(thrift_protocol.TBinaryProtocol(buffer))
        source = "import base64,urllib.request; urllib.request.urlopen(urllib.request.Request('http://otel-collector.observe:14268/api/traces',data=base64.b64decode(%r),headers={'Content-Type':'application/x-thrift'}),timeout=20).read()"
    kubectl(
        "-n",
        pod["metadata"]["namespace"],
        "exec",
        "frontend",
        "--",
        "python",
        "-c",
        source % base64.b64encode(buffer.getvalue()).decode(),
    )
    return f"{identity:016x}"


def send_otlp(stub, operation, attributes):
    types = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    request = types.ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    for key, value in {"service.name": "frontend", **attributes}.items():
        attribute = resource.resource.attributes.add(key=key).value
        if isinstance(value, int):
            attribute.int_value = value
        else:
            attribute.string_value = value
    span = resource.scope_spans.add().spans.add(name=operation, kind=2)
    identity = uuid.uuid4()
    span.trace_id, span.span_id = identity.bytes, uuid.uuid4().bytes[:8]
    span.start_time_unix_nano = time.time_ns()
    span.end_time_unix_nano = span.start_time_unix_nano + 1_000_000
    response = stub.Export(request, timeout=20)
    if hasattr(response, "partial_success"):
        assert response.partial_success.rejected_spans == 0
    return identity.hex


def test_trace_identity_and_replica_counters(endpoints, pods, tmp_path):
    grpc = pytest.importorskip("grpc")
    service = pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2_grpc")
    otlp, prometheus, jaeger = endpoints
    prefix = f"namespace-test-{uuid.uuid4().hex}"
    expected, trace_ids = {}, []
    with grpc.insecure_channel(otlp) as channel:
        stub = service.TraceServiceStub(channel)
        for pod in pods:
            namespace, uid, ip = pod["metadata"]["namespace"], pod["metadata"]["uid"], pod["status"]["podIP"]
            for protocol in ("http", "udp"):
                operation = f"{prefix}-{protocol}"
                trace_ids.append(send_jaeger(pod, operation, protocol))
                expected[namespace, operation] = 1
            for attribute, value in (("k8s.pod.uid", uid), ("k8s.pod.ip", ip)):
                operation = f"{prefix}-{attribute}"
                trace_ids.append(send_otlp(stub, operation, {attribute: value}))
                expected[namespace, operation] = 1
        # Different resource identities must aggregate into one cumulative series.
        namespace = pods[0]["metadata"]["namespace"]
        for pid in (101, 202):
            trace_ids.append(send_jaeger(pods[0], f"{prefix}-jaeger-replicas", "http", pid=pid))
            trace_ids.append(
                send_otlp(stub, f"{prefix}-replicas", {"k8s.namespace.name": namespace, "process.pid": pid})
            )
        expected[namespace, f"{prefix}-replicas"] = 2
        expected[namespace, f"{prefix}-jaeger-replicas"] = 2
        trace_ids.append(send_otlp(stub, f"{prefix}-unknown", {}))
        expected[None, f"{prefix}-unknown"] = 1
        trace_ids.append(send_otlp(stub, f"{prefix}-forwarded", {"k8s.namespace.name": "forwarded-app"}))
        expected["forwarded-app", f"{prefix}-forwarded"] = 1

    query = f'sum by (namespace,span_name) (traces_span_metrics_calls_total{{span_name=~"{prefix}.*"}})'
    deadline, actual = time.monotonic() + 100, {}
    while time.monotonic() < deadline:
        response = requests.get(f"{prometheus}/api/v1/query", params={"query": query}, timeout=20)
        response.raise_for_status()
        data = response.json()
        (tmp_path / "metrics.json").write_text(json.dumps(data, indent=2))
        assert data["status"] == "success", data
        actual = {
            (r["metric"].get("namespace"), r["metric"]["span_name"]): float(r["value"][1])
            for r in data["data"]["result"]
        }
        if actual == expected:
            break
        time.sleep(5)
    assert actual == expected
    # Both receivers sent two 1 ms spans for each selected operation.
    # A nanosecond bucket typo leaves counts/sums correct but clips quantiles.
    for suffix in ("http", "k8s.pod.uid"):
        selector = f'{{span_name="{prefix}-{suffix}"}}'
        results = {}
        for key, query in {
            "count": f"sum(traces_span_metrics_duration_milliseconds_count{selector})",
            "sum": f"sum(traces_span_metrics_duration_milliseconds_sum{selector})",
            "median": f"histogram_quantile(0.5, sum by(le)(traces_span_metrics_duration_milliseconds_bucket{selector}))",
        }.items():
            response = requests.get(f"{prometheus}/api/v1/query", params={"query": query}, timeout=20)
            response.raise_for_status()
            data = response.json()
            (tmp_path / f"histogram-{suffix}-{key}.json").write_text(json.dumps(data, indent=2))
            assert data["status"] == "success" and len(data["data"]["result"]) == 1, data
            results[key] = float(data["data"]["result"][0]["value"][1])
        assert results["count"] == 2 and results["sum"] == 2, results
        assert 0.5 <= results["median"] <= 5, results
    for identity in trace_ids:
        response = requests.get(f"{jaeger}/api/traces/{identity}", timeout=20)
        response.raise_for_status()
        data = response.json()
        (tmp_path / f"trace-{identity}.json").write_text(json.dumps(data, indent=2))
        assert len(data.get("data", [])) == 1
