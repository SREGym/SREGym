"""Tests for the Harbor sidecar backend with a fake problem session."""

import hashlib
import json
import os
import urllib.error
import urllib.request

import pytest

from sregym.harbor import protocol
from sregym.harbor.backend import CLUSTER_SNAPSHOT_NAME, Backend

TOKEN = "reference-solution-token"
GRADE_TOKEN = "grade-token"


class FakeSession:
    def __init__(self, tmp_path, *, fail_setup=False, settles_after=1, fault_seen_after=1, unseen_verdict=None):
        self.tmp_path = tmp_path
        self.fail_setup = fail_setup
        # The oracle passes from this many checks after recovery on, as when
        # alerts take a while to clear. None: never.
        self.settles_after = settles_after
        # The oracle sees the fault from this many checks after injection on,
        # as when alerts take a while to fire. None: never. Until then it
        # returns unseen_verdict.
        self.fault_seen_after = fault_seen_after
        self.unseen_verdict = unseen_verdict or {"success": True, "reason": None}
        self.recovered = False
        self.grades = 0
        self.grades_since_recovery = 0

    def setup(self) -> str:
        if self.fail_setup:
            raise RuntimeError("deploy failed")
        kubeconfig = self.tmp_path / "agent-kubeconfig"
        kubeconfig.write_text("kubeconfig")
        return str(kubeconfig)

    def grade(self) -> dict:
        self.grades += 1
        if not self.recovered:
            if self.fault_seen_after is None or self.grades < self.fault_seen_after:
                return dict(self.unseen_verdict)
            return {"success": False, "reason": "fault_present", "failure_class": "agent_error"}
        self.grades_since_recovery += 1
        if self.settles_after is None or self.grades_since_recovery < self.settles_after:
            return {"success": False, "reason": "alerts_still_firing"}
        return {"success": True, "reason": None}

    def recover(self) -> None:
        self.recovered = True

    def close(self) -> None:
        pass


@pytest.fixture
def make_backend(tmp_path):
    def make(recovery_settle_timeout_s=60, fault_live_timeout_s=60, **session_options):
        session = FakeSession(tmp_path, **session_options)
        backend = Backend(
            session,
            problem_id="some_problem",
            shared_dir=tmp_path / "shared",
            output_dir=tmp_path / "output",
            oracle_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
            grade_token=GRADE_TOKEN,
            recovery_settle_timeout_s=recovery_settle_timeout_s,
            recovery_settle_interval_s=0,
            fault_live_timeout_s=fault_live_timeout_s,
            fault_live_interval_s=0,
        )
        return backend, session

    return make


def _state(backend) -> str:
    return (backend.shared_dir / protocol.STATE_NAME).read_text().strip()


def test_setup_publishes_kubeconfig_and_ready_state(make_backend):
    backend, _ = make_backend()
    assert backend.setup()
    assert _state(backend) == protocol.STATE_READY
    kubeconfig = backend.shared_dir / protocol.KUBECONFIG_NAME
    assert kubeconfig.read_text() == "kubeconfig"
    assert kubeconfig.stat().st_mode & 0o044  # readable by any agent user
    status = (backend.shared_dir / protocol.STATUS_NAME).read_text()
    assert "some_problem" not in status


def test_failed_setup_is_reported_and_graded_as_an_error(make_backend):
    backend, _ = make_backend(fail_setup=True)
    assert not backend.setup()
    assert _state(backend) == protocol.STATE_FAILED
    assert "deploy failed" in json.loads((backend.shared_dir / protocol.STATUS_NAME).read_text())["error"]
    grade = backend.grade()
    assert grade["success"] is False and "mitigation" not in grade
    with pytest.raises(RuntimeError):
        backend.recover()


def test_grade_runs_the_oracle_once_and_persists_the_verdict(make_backend):
    backend, session = make_backend()
    backend.setup()
    first = backend.grade()
    assert backend.grade() is first
    assert session.grades == 2  # one check during setup, one grade
    assert first["success"] is False
    assert first["mitigation"]["reason"] == "fault_present"
    saved = json.loads((backend.output_dir / "grade.json").read_text())
    assert saved["problem_id"] == "some_problem"
    # Grading is final: the reference solution cannot run afterwards.
    with pytest.raises(RuntimeError, match="graded"):
        backend.recover()


def test_recovery_then_grade_succeeds(make_backend):
    backend, _ = make_backend()
    backend.setup()
    assert backend.recover()["recovered"] is True
    assert backend.grade()["success"] is True


def test_recovery_waits_until_the_oracle_passes(make_backend):
    backend, session = make_backend(settles_after=3)
    backend.setup()
    result = backend.recover()
    assert (result["settled"], result["checks"]) == (True, 3)
    # Checks during recovery are not the grade: the verifier still runs one.
    assert backend.grade()["success"] is True
    assert session.grades == 5  # one during setup, three during recovery, the grade


def test_setup_waits_until_the_oracle_sees_the_fault(make_backend):
    backend, session = make_backend(fault_seen_after=3)
    assert backend.setup()
    assert session.grades == 3
    # Checks during setup are not the grade.
    assert backend.grade()["mitigation"]["reason"] == "fault_present"


@pytest.mark.parametrize(
    "unseen_verdict",
    [
        {"success": True},
        {"success": False, "reason": "prometheus_unreachable", "failure_class": "environment_error"},
        {"success": False, "reason": "oracle_raised", "failure_class": "harness_error"},
    ],
)
def test_setup_fails_when_the_oracle_never_sees_the_fault(make_backend, unseen_verdict):
    backend, _ = make_backend(fault_seen_after=None, unseen_verdict=unseen_verdict, fault_live_timeout_s=0)
    assert not backend.setup()
    assert _state(backend) == protocol.STATE_FAILED
    error = json.loads((backend.shared_dir / protocol.STATUS_NAME).read_text())["error"]
    assert "had not seen the fault" in error
    assert ("any agent would" in error) == unseen_verdict["success"]
    if not unseen_verdict["success"]:
        assert unseen_verdict["reason"] in error


def test_recovery_stops_waiting_at_the_timeout(make_backend):
    backend, _ = make_backend(settles_after=None, recovery_settle_timeout_s=0)
    backend.setup()
    result = backend.recover()
    assert (result["recovered"], result["settled"], result["checks"]) == (True, False, 1)
    assert backend.grade()["mitigation"]["reason"] == "alerts_still_firing"


def test_steady_state_comes_from_the_task_environment(monkeypatch):
    from sregym.harbor.backend import parse_args

    monkeypatch.setenv(protocol.PROBLEM_ID_ENV, "some_problem")
    monkeypatch.delenv(protocol.STEADY_STATE_ENV, raising=False)
    assert parse_args([]).steady_state_s == 0
    monkeypatch.setenv(protocol.STEADY_STATE_ENV, "300")
    assert parse_args([]).steady_state_s == 300


def test_alert_graded_problems_ignore_hidden_workloads():
    from types import SimpleNamespace

    from sregym.conductor.oracles.alert_oracle import AlertOracle
    from sregym.harbor.backend import grade_visible_alerts_only

    problem = SimpleNamespace(namespace="astronomy-shop")
    problem.mitigation_oracle = AlertOracle(problem=problem)
    grade_visible_alerts_only(problem)
    assert problem.mitigation_oracle.ignore_hidden_workloads is True
    # Other oracles are left alone.
    grade_visible_alerts_only(SimpleNamespace(mitigation_oracle=object()))
    grade_visible_alerts_only(SimpleNamespace())


@pytest.mark.parametrize(
    ("header", "valid"),
    [
        (f"Bearer {TOKEN}", True),
        (f"bearer {TOKEN}", True),
        ("Bearer wrong", False),
        (TOKEN, False),
        (None, False),
    ],
)
def test_token_validation(make_backend, header, valid):
    backend, _ = make_backend()
    assert backend.token_is_valid(header) is valid


def test_recovery_is_disabled_without_a_configured_token(tmp_path):
    backend = Backend(
        FakeSession(tmp_path),
        problem_id="p",
        shared_dir=tmp_path / "shared",
        output_dir=tmp_path / "output",
        oracle_token_sha256=None,
    )
    assert not backend.token_is_valid(f"Bearer {TOKEN}")


def _request(port: int, method: str, path: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_routes(make_backend):
    backend, _ = make_backend()
    backend.setup()
    backend.start_servers(api_host="127.0.0.1", api_port=0, grade_port=0)
    try:
        public, local = (server.server_address[1] for server in backend._servers)
        assert _request(public, "GET", "/status") == (200, {"state": protocol.STATE_READY})
        # Grading is only exposed on the loopback listener used by Harbor's collect hook.
        assert _request(public, "POST", "/grade")[0] == 404
        assert _request(public, "POST", "/oracle/recover")[0] == 403
        assert _request(public, "POST", "/oracle/recover", token="wrong")[0] == 403
        assert _request(public, "POST", "/oracle/recover", token=TOKEN)[0] == 200
        # The agent may share the loopback; grading needs the root-only grade token.
        assert _request(local, "POST", "/grade")[0] == 403
        assert _request(local, "POST", "/grade", token=TOKEN)[0] == 403
        status, grade = _request(local, "POST", "/grade", token=GRADE_TOKEN)
        assert status == 200 and grade["success"] is True
        assert _request(public, "POST", "/oracle/recover", token=TOKEN)[0] == 409
    finally:
        backend.stop_servers()


def test_failed_setup_records_the_cluster_state(make_backend, tmp_path, monkeypatch):
    # A stand-in kubectl: one unready pod, and each call echoes its arguments.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    pods = {
        "items": [
            {
                "metadata": {"namespace": "app", "name": "ok"},
                "status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
            },
            {"metadata": {"namespace": "app", "name": "stuck"}, "status": {"phase": "Pending"}},
        ]
    }
    kubectl.write_text(
        "#!/bin/sh\n"
        f"if [ \"$*\" = 'get pods -A -o json' ]; then echo '{json.dumps(pods)}'; else echo \"kubectl $*\"; fi\n"
    )
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    backend, _ = make_backend(fail_setup=True)
    assert not backend.setup()
    snapshot = (backend.output_dir / "logs" / CLUSTER_SNAPSHOT_NAME).read_text()
    assert "kubectl get pods -A -o wide" in snapshot
    assert "kubectl describe pod -n app stuck" in snapshot
    assert "logs -n app stuck --all-containers --tail=80 --previous" in snapshot
    assert "-n app ok" not in snapshot
