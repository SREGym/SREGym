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
