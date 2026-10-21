from pathlib import Path

import pytest
import yaml

import sregym.generators.workload.incident_arena as workload_module
from sregym.generators.workload.incident_arena import IncidentArenaLoadgen
from sregym.service.apps.saleor.application import Saleor
from sregym.service.helm import Helm


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

    def status(self, timeout=180):
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step

    def restart(self, timeout_s=600):
        self.restarts += 1

    def start_episode(self, timeout=180):
        self.starts += 1
        return {"status": 202, "body": "{}"}

    def logs(self, tail=40):
        raise AssertionError("Do not collect logs after the deadline")


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


def test_wait_for_traffic_times_out_with_the_cached_pod_logs(clock):
    loadgen = ScriptedLoadgen([{**STARTING, "log_tail": ["sidecar stdout"]}])
    with pytest.raises(RuntimeError, match="no traffic within 60s") as raised:
        loadgen.wait_for_traffic(timeout_s=60)
    assert "episode still starting" in str(raised.value)
    assert "last episode-start response" in str(raised.value)
    assert "sidecar stdout" in str(raised.value)
    assert clock.now == 60


def test_retry_delay_cannot_start_a_restart_after_the_deadline(clock):
    loadgen = ScriptedLoadgen([FAILED])
    with pytest.raises(RuntimeError, match="no traffic within 20s"):
        loadgen.wait_for_traffic(timeout_s=20)
    assert clock.now == 20
    assert loadgen.restarts == 0


def test_slow_restart_uses_only_the_remaining_budget(clock, monkeypatch):
    loadgen = ScriptedLoadgen([FAILED])
    budgets = []

    def restart(timeout_s):
        budgets.append(timeout_s)
        clock.sleep(timeout_s)

    monkeypatch.setattr(loadgen, "restart", restart)
    with pytest.raises(RuntimeError, match="no traffic within 60s"):
        loadgen.wait_for_traffic(timeout_s=60)
    assert budgets == [30]
    assert clock.now == 60


@pytest.mark.parametrize("step", ["status", "start_episode"])
def test_slow_ledger_commands_cannot_outlive_the_wait(clock, monkeypatch, step):
    loadgen = ScriptedLoadgen([STARTING])
    budgets = []

    def slow_command(timeout):
        budgets.append(timeout)
        clock.sleep(timeout)
        raise TimeoutError("command timed out")

    monkeypatch.setattr(loadgen, step, slow_command)
    with pytest.raises(RuntimeError, match="no traffic within 25s"):
        loadgen.wait_for_traffic(timeout_s=25)
    assert budgets == [25]
    assert clock.now == 25


def test_restart_shares_a_deadline_between_its_commands(clock):
    calls = []

    class Kubectl:
        def exec_command_checked(self, command, timeout):
            calls.append((command, timeout))
            clock.sleep(4)

    IncidentArenaLoadgen("ns", Kubectl()).restart(timeout_s=10)
    assert calls[0][1] == 10
    assert calls[1][1] == 6
    assert "--timeout=6s" in calls[1][0]


def test_status_and_episode_start_forward_the_command_budget(monkeypatch):
    loadgen = IncidentArenaLoadgen("ns", None)
    calls = []

    def ledger(mode, *, timeout):
        calls.append((mode, timeout))
        return {}

    monkeypatch.setattr(loadgen, "_run_ledger", ledger)
    loadgen.status(timeout=7)
    loadgen.start_episode(timeout=5)
    assert calls == [("status", 7), ("start", 5)]


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


def test_saleor_waits_for_initialization_before_starting_consumers(offline_cluster, monkeypatch):
    app = Saleor()
    app.configure({"saleor": {"api": {"replicaCount": 3}}})
    events = []
    app.kubectl.create_namespace_if_not_exist = lambda namespace: None
    monkeypatch.setattr(
        Helm,
        "install",
        lambda **cfg: events.append(
            ("install", yaml.safe_load(Path(cfg["extra_args"][-1]).read_text()), cfg["extra_args"][-1])
        ),
    )
    monkeypatch.setattr(app, "wait_for_jobs", lambda timeout: events.append(("jobs", timeout)))
    monkeypatch.setattr(app, "wait_until_ready", lambda: events.append(("ready",)))
    original = app._apply_release

    def apply(values, *, install):
        if install:
            original(values, install=True)
        else:
            events.append(("upgrade", values))

    monkeypatch.setattr(app, "_apply_release", apply)
    app.deploy()
    assert [e[0] for e in events] == ["install", "jobs", "upgrade", "ready"]
    assert events[0][1]["saleor"] == {"api": {"replicaCount": 0}, "worker": {"replicaCount": 0}}
    assert events[2][1]["saleor"]["api"]["replicaCount"] == 3
    assert not Path(events[0][2]).exists()


def test_failed_saleor_initialization_does_not_start_consumers(offline_cluster, monkeypatch):
    app = Saleor()
    app.kubectl.create_namespace_if_not_exist = lambda namespace: None
    events = []
    monkeypatch.setattr(app, "_apply_release", lambda values, *, install: events.append(install))

    def fail(timeout):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(app, "wait_for_jobs", fail)
    with pytest.raises(RuntimeError, match="migration failed"):
        app.deploy()
    assert events == [True]
