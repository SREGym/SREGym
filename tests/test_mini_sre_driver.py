import pytest

from clients.mini_sre import driver, tools
from clients.mini_sre.backends import Reply


def test_is_external_submit():
    assert driver.is_external_submit("curl -X POST http://localhost:8000/submit -d '{}'")
    assert driver.is_external_submit("python3 -c \"requests.post('http://h:8000/submit_mcp')\"")
    assert not driver.is_external_submit("kubectl get pods -n submitter")


def test_run_command_captures_output_and_exit_code(tmp_path):
    result = tools.run_command("echo hi; echo err 1>&2; exit 3", timeout=5, cwd=str(tmp_path))
    assert result.stdout == "hi\n" and result.stderr == "err\n" and result.exit_code == 3
    slow = tools.run_command("sleep 5", timeout=1)
    assert slow.exit_code == 124 and slow.timed_out


class OneReply:
    def __init__(self, reply):
        self.reply = reply

    @staticmethod
    def system_message(text):
        return {"role": "system", "content": text}

    def complete(self, messages, *, step_dir):
        return self.reply


def test_preflight_exits_zero_on_ok_reply(monkeypatch):
    monkeypatch.setattr(driver, "MODEL", "fake-model")
    monkeypatch.setattr(driver, "make_backend", lambda model, effort: OneReply(Reply(content="ok")))
    with pytest.raises(SystemExit) as exc:
        driver.run_preflight()
    assert exc.value.code == 0
    monkeypatch.setattr(driver, "make_backend", lambda model, effort: OneReply(Reply(error="model call failed: nope")))
    with pytest.raises(SystemExit) as exc:
        driver.run_preflight()
    assert exc.value.code == 1
    monkeypatch.setattr(driver, "MODEL", "")
    with pytest.raises(SystemExit) as exc:
        driver.run_preflight()
    assert exc.value.code == 1


class Replies:
    """A backend that gives its replies in turn."""

    def __init__(self, *contents):
        self.contents = list(contents)

    def complete(self, messages, *, step_dir):
        return Reply(content=self.contents.pop(0))


def bash(command):
    return f"```bash\n{command}\n```"


def run_mitigation(tmp_path, monkeypatch, statuses, *replies):
    """Run a mitigation stage against a conductor whose /status answers come from ``statuses`` in turn."""
    answers = iter(statuses)
    monkeypatch.setattr(driver, "conductor_status", lambda: next(answers, statuses[-1]))
    monkeypatch.setattr(driver, "DEADLINE_S", 1500.0)
    session = driver.Session()
    session.open({"role": "system", "content": "s"}, "task")
    outcome = driver.run_stage(
        Replies(*replies),
        session,
        driver.Transcript(tmp_path / "t.jsonl"),
        tmp_path / "steps",
        stage="mitigation",
        work_dir=str(tmp_path),
    )
    return outcome, [m["content"] for m in session.messages if m["role"] == "user"]


def test_time_left_is_what_is_left_of_the_whole_attempt(tmp_path, monkeypatch):
    # a long diagnosis left 30 s of SREGym's 1800 s: the model is told so, not the stage's own 1500 s
    status = {"stage": "mitigation", "agent_remaining_seconds": 30.0}
    _, said = run_mitigation(
        tmp_path, monkeypatch, [status, status, {"stage": "done"}], bash("echo one"), bash("echo two")
    )
    assert "about 0 minutes left in this stage" in said[1] and "24 minutes" not in said[1]
    # with plenty of time left of the attempt, the stage's own limit counts
    roomy = {"stage": "mitigation", "agent_remaining_seconds": 1700.0}
    _, said = run_mitigation(
        tmp_path, monkeypatch, [roomy, roomy, {"stage": "done"}], bash("echo one"), bash("echo two")
    )
    assert "about 24 minutes left in this stage" in said[1]


def test_no_time_left_of_the_attempt_asks_for_a_submission(tmp_path, monkeypatch):
    gone = {"stage": "mitigation", "agent_remaining_seconds": 0.0}
    outcome, said = run_mitigation(tmp_path, monkeypatch, [gone], *[bash("echo x")] * 5)
    assert said[1].startswith("You have reached the time limit")
    assert outcome.termination_reason == "deadline_no_submission" and outcome.commands_used == driver.WRAP_UP_CALLS


def test_without_a_time_from_the_conductor_the_stage_limit_counts(tmp_path, monkeypatch):
    # an older conductor, or one that does not answer
    _, said = run_mitigation(tmp_path, monkeypatch, [None, {"stage": "done"}], bash("echo one"))
    assert "about 24 minutes left in this stage" in said[1]
