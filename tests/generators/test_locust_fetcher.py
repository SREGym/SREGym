import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from sregym.generators.workload.locust import LocustWorkloadManager


def run_collection(monkeypatch, capsys, responses, rounds=1):
    template = Path(__file__).resolve().parents[2] / "sregym/generators/workload/locust-fetcher-template.yaml"
    container = yaml.safe_load(template.read_text())["spec"]["containers"][0]
    assert container["command"] == ["python", "-u", "-c"]
    source = container["args"][0]
    assert "apt-get" not in source and "curl" not in source
    monkeypatch.setenv("LOCUST_URL", "http://load-generator:8089")
    monkeypatch.setenv("INTERVAL_SECONDS", "10")
    requests = []

    def request(url, timeout):
        requests.append((url, timeout))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)

    def end_round(seconds):
        nonlocal rounds
        assert seconds == 10
        rounds -= 1
        if rounds == 0:
            raise KeyboardInterrupt

    monkeypatch.setattr("urllib.request.urlopen", request)
    monkeypatch.setattr("time.sleep", end_round)
    with pytest.raises(KeyboardInterrupt):
        exec(compile(source, str(template), "exec"), {})
    return requests, capsys.readouterr()


def test_collector_keeps_existing_record_format_and_reset(monkeypatch, capsys):
    stats = {"stats": [{"safe_name": "Aggregated", "num_requests": 20}], "errors": []}
    requests, output = run_collection(monkeypatch, capsys, [json.dumps(stats).encode(), b"OK"])
    assert requests == [("http://load-generator:8089/stats/requests", 5), ("http://load-generator:8089/stats/reset", 5)]
    lines = output.out.splitlines()
    assert len(lines) == 2 and not output.err
    parsed = LocustWorkloadManager.__new__(LocustWorkloadManager)._parse_log(
        [{"content": line, "time": "2026-09-05T19:00:00.123456Z"} for line in lines]
    )
    assert parsed.number == 20 and parsed.ok


@pytest.mark.parametrize("responses", [[TimeoutError("timeout")], [b"not json"], [b"{}", TimeoutError("reset failed")]])
def test_collection_errors_do_not_emit_a_successful_sample(monkeypatch, capsys, responses):
    _, output = run_collection(monkeypatch, capsys, responses)
    assert output.out == "Running Locust on round #0\n"
    assert "Locust collection failed:" in output.err


def test_failed_round_preserves_adjacent_successful_samples(monkeypatch, capsys):
    # Kubernetes merges stdout and stderr in the pod log stream.
    monkeypatch.setattr(sys, "stderr", sys.stdout)
    stats = json.dumps({"stats": [{"safe_name": "Aggregated", "num_requests": 20}], "errors": []}).encode()
    _, output = run_collection(
        monkeypatch, capsys, [stats, b"OK", TimeoutError("timeout"), stats, b"OK", TimeoutError("timeout")], rounds=4
    )
    logs = "\n".join(f"2026-09-05T19:00:00.{index:09d}Z {line}" for index, line in enumerate(output.out.splitlines()))
    manager = LocustWorkloadManager.__new__(LocustWorkloadManager)
    manager.namespace = "shop"
    manager.log_pool = []
    manager.last_log_line_time = None
    manager.core_v1_api = SimpleNamespace(
        list_namespaced_pod=lambda *_args, **_kwargs: SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name="collector"))]
        ),
        read_namespaced_pod_log=lambda *_args, **_kwargs: logs,
    )
    samples = manager.retrievelog()
    assert len(samples) == 2
    assert all(sample.number == 20 and sample.ok for sample in samples)
