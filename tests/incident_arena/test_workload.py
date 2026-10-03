import pytest

import sregym.generators.workload.incident_arena as workload_module
from sregym.generators.workload.incident_arena import IncidentArenaLoadgen
from sregym.service.apps.incident_arena.saleor import Saleor


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ScriptedLoadgen(IncidentArenaLoadgen):
    """A load generator whose in-pod status follows a script, one entry per poll."""

    def __init__(self, script):
        super().__init__("ns", kubectl=None)
        self.script = list(script)
        self.restarts = 0
        self.starts = 0

    def status(self):
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step

    def restart(self, timeout_s=600):
        self.restarts += 1

    def start_episode(self):
        self.starts += 1
        return {"status": 202, "body": "{}"}

    def logs(self, tail=40):
        return "sidecar stdout"


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(workload_module, "time", fake)
    return fake


STARTING = {"latest_sent_s": None, "episode_done": None, "log_tail": None}
FAILED = {
    "latest_sent_s": None,
    "episode_done": {"done": False, "error": "RuntimeError: ZERO purchasable variants"},
    "log_tail": ["provisioning VariantCatalog", "loadgen sidecar episode FAILED"],
}
SENDING = {"latest_sent_s": 12.5, "episode_done": None, "log_tail": None}


def test_wait_for_traffic_starts_the_episode_until_traffic_flows(clock):
    loadgen = ScriptedLoadgen([RuntimeError("container not running"), STARTING, STARTING, SENDING])
    assert loadgen.wait_for_traffic() == 12.5
    assert loadgen.starts == 2
    assert loadgen.restarts == 0


def test_wait_for_traffic_is_a_single_read_once_traffic_flows(clock):
    loadgen = ScriptedLoadgen([SENDING])
    assert loadgen.wait_for_traffic() == 12.5
    assert loadgen.starts == 0
    assert clock.now == 0.0


def test_wait_for_traffic_restarts_an_episode_that_failed_to_start(clock):
    loadgen = ScriptedLoadgen([STARTING, FAILED, STARTING, SENDING])
    assert loadgen.wait_for_traffic() == 12.5
    assert loadgen.restarts == 1


def test_wait_for_traffic_reports_the_sidecar_error_after_its_restarts(clock):
    loadgen = ScriptedLoadgen([FAILED])
    with pytest.raises(RuntimeError, match="ZERO purchasable variants") as raised:
        loadgen.wait_for_traffic(max_restarts=2)
    assert loadgen.restarts == 2
    assert "loadgen sidecar episode FAILED" in str(raised.value)


def test_wait_for_traffic_times_out_with_the_pod_logs(clock):
    loadgen = ScriptedLoadgen([STARTING])
    with pytest.raises(RuntimeError, match="no traffic within 60s") as raised:
        loadgen.wait_for_traffic(timeout_s=60)
    assert "episode still starting" in str(raised.value)
    assert "last episode-start response" in str(raised.value)
    assert "sidecar stdout" in str(raised.value)


class JobsKubeCtl:
    def __init__(self, jobs):
        self.jobs = jobs
        self.commands = []

    def exec_command_checked(self, command, input_data=None, timeout=None):
        self.commands.append(command)
        return "".join(f"job.batch/{job}\n" for job in self.jobs) if command.startswith("kubectl get jobs") else ""


def test_wait_for_jobs_waits_for_every_seed_job(offline_cluster):
    app = Saleor()
    app.kubectl = JobsKubeCtl(["saleor-init"])
    app.wait_for_jobs(300)
    assert app.kubectl.commands[-1] == (
        "kubectl wait --for=condition=complete job.batch/saleor-init -n saleor --timeout=300s"
    )


def test_wait_for_jobs_without_jobs_is_a_no_op(offline_cluster):
    app = Saleor()
    app.kubectl = JobsKubeCtl([])
    app.wait_for_jobs(300)
    assert len(app.kubectl.commands) == 1
