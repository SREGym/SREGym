"""Tests for the Harbor sidecar backend with a fake problem session."""

import hashlib
import json
import urllib.error
import urllib.request

import pytest

from sregym.harbor import protocol
from sregym.harbor.backend import Backend

TOKEN = "reference-solution-token"


class FakeSession:
    def __init__(self, tmp_path, *, fail_setup=False):
        self.tmp_path = tmp_path
        self.fail_setup = fail_setup
        self.recovered = False
        self.grades = 0

    def setup(self) -> str:
        if self.fail_setup:
            raise RuntimeError("deploy failed")
        kubeconfig = self.tmp_path / "agent-kubeconfig"
        kubeconfig.write_text("kubeconfig")
        return str(kubeconfig)

    def grade(self) -> dict:
        self.grades += 1
        return {"success": self.recovered, "reason": None if self.recovered else "fault_present"}

    def recover(self) -> None:
        self.recovered = True

    def close(self) -> None:
        pass


@pytest.fixture
def make_backend(tmp_path):
    def make(**session_options):
        session = FakeSession(tmp_path, **session_options)
        backend = Backend(
            session,
            problem_id="some_problem",
            shared_dir=tmp_path / "shared",
            output_dir=tmp_path / "output",
            oracle_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
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
    assert session.grades == 1
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
        status, grade = _request(local, "POST", "/grade")
        assert status == 200 and grade["success"] is True
        assert _request(public, "POST", "/oracle/recover", token=TOKEN)[0] == 409
    finally:
        backend.stop_servers()
