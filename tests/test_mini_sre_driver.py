import time

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


def run_one_stage(tmp_path, monkeypatch, statuses, *replies, stage="mitigation", left=1800.0, stage_left=None):
    """Run one stage against a conductor whose /status answers come from ``statuses`` in turn.

    ``left`` is what is left of the attempt when the stage starts, ``stage_left`` what is left of the stage's share.
    """
    answers = iter(statuses)
    monkeypatch.setattr(driver, "conductor_status", lambda: next(answers, statuses[-1]))
    session = driver.Session()
    session.open({"role": "system", "content": "s"}, "task")
    now = time.monotonic()
    outcome = driver.run_stage(
        Replies(*replies),
        session,
        driver.Transcript(tmp_path / "t.jsonl"),
        tmp_path / "steps",
        stage=stage,
        work_dir=str(tmp_path),
        run_ends=now + left,
        stage_ends=None if stage_left is None else now + stage_left,
    )
    return outcome, [m["content"] for m in session.messages if m["role"] == "user"]


def test_diagnosis_may_use_two_thirds_of_the_attempt():
    assert driver.diagnosis_deadline(1800.0, 0.0) == pytest.approx(1200.0)
    # a longer --agent-timeout gives both stages more: diagnosis up to 2400 of 3600 seconds
    assert driver.diagnosis_deadline(3600.0, 0.0) == pytest.approx(2400.0)


def test_diagnosis_is_told_the_time_left_of_its_share(tmp_path, monkeypatch):
    # 1190 s are left of diagnosis's share, 1790 s of the attempt; the submission moves the conductor on
    moved_on = {"stage": "mitigation", "agent_remaining_seconds": 1789.0}
    _, said = run_one_stage(
        tmp_path, monkeypatch, [moved_on], bash("echo one"), stage="diagnosis", left=1790.0, stage_left=1190.0
    )
    assert "about 19 minutes left in this stage" in said[1]


def test_mitigation_is_told_what_is_left_of_the_whole_attempt(tmp_path, monkeypatch):
    # a long diagnosis left 30 s of SREGym's 1800 s: the model is told so
    status = {"stage": "mitigation", "agent_remaining_seconds": 30.0}
    _, said = run_one_stage(
        tmp_path, monkeypatch, [status, {"stage": "done"}], bash("echo one"), bash("echo two"), left=30.0
    )
    assert "about 0 minutes left in this stage" in said[1] and "24 minutes" not in said[1]


def test_no_time_left_asks_for_a_submission(tmp_path, monkeypatch):
    gone = {"stage": "mitigation", "agent_remaining_seconds": 0.0}
    outcome, said = run_one_stage(tmp_path, monkeypatch, [gone], *[bash("echo x")] * 5, left=0.0)
    assert said[1].startswith("You have reached the time limit")
    assert outcome.termination_reason == "deadline_no_submission" and outcome.commands_used == driver.WRAP_UP_CALLS


def test_the_time_left_is_read_again_after_each_command(tmp_path, monkeypatch):
    # the stage started with 1800 s, but the conductor now says 100 s are left
    late = {"stage": "mitigation", "agent_remaining_seconds": 100.0}
    _, said = run_one_stage(tmp_path, monkeypatch, [late, {"stage": "done"}], bash("echo one"), bash("echo two"))
    assert "about 1 minute left in this stage" in said[1]
