import json
import sys
from pathlib import Path

import pytest

from clients.harness.token_usage import usage_metrics
from clients.mini_sre import driver, mini, tools
from clients.mini_sre.backends import Reply
from sregym.run_artifacts import _find_token_hit

APP = {"app_name": "Hotel Reservation", "namespace": "hotel-reservation", "descriptions": "A hotel app."}
REAL_PROBLEM_ID = "service_wrong_pod_selection_hotel_reservation"
MARKER = mini.MARKER
CURL_DIAG = 'curl -s -X POST http://localhost:8000/submit -d \'{"solution": "the selector is wrong"}\''
CURL_FIX = 'curl -s -X POST http://localhost:8000/submit -d \'{"solution": ""}\''


class FakeBackend:
    name = "api"
    model = "fake-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def version(self):
        return "fake-1"

    @staticmethod
    def system_message(text):
        return {"role": "system", "content": text}

    def complete(self, messages, *, step_dir):
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "messages.json").write_text(json.dumps(messages))
        self.calls.append(json.loads(json.dumps(messages)))
        reply = self.replies.pop(0)
        if isinstance(reply, str):
            reply = Reply(content=reply, reasoning="thinking")
        if reply.usage.get("input_tokens") is None:
            reply.usage = usage_metrics(
                input_tokens=100, output_tokens=10, cached_input_tokens=50, reasoning_output_tokens=5
            )
        return reply


def block(command, thought="THOUGHT: look"):
    return f"{thought}\n\n```bash\n{command}\n```\n"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    state = {"stages": ["diagnosis", "done"], "commands": [], "logs": tmp_path, "current": None}
    monkeypatch.setattr(driver, "AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(driver, "MODEL", "fake-model")
    monkeypatch.setattr(driver, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(driver, "resolve_problem_id", lambda cli_problem_id=None: "anon_test")
    monkeypatch.setattr(driver, "get_app_info", lambda: APP)
    monkeypatch.setattr(driver, "conductor_status", lambda: {"stage": state["current"]})

    def wait_for_stage(stages, timeout):
        state["current"] = state["stages"].pop(0)
        return state["current"]

    monkeypatch.setattr(driver, "wait_for_stage", wait_for_stage)

    def run_command(cmd, timeout, cwd=None):
        state["commands"].append(cmd)
        if "/submit" in cmd:
            stage = state["current"]
            if not state.get("conductor_evaluates_slowly"):  # if set, the stage changes later, like in a real run
                state["current"] = "mitigation" if stage == "diagnosis" else "done"
            receipt = f'{{"status":"200","message":"Submission received","stage":"{stage}"}}\n'
            return tools.CommandResult(receipt, "", 0, 0.05)
        if cmd.startswith("sleep"):
            return tools.CommandResult("", "", 124, 60.0, timed_out=True)
        return tools.CommandResult("pod-a Running\n", "warn\n", 0, 0.01)

    monkeypatch.setattr(driver, "run_command", run_command)
    monkeypatch.setattr(sys, "argv", ["driver"])
    return state


def run_main(monkeypatch, backend, *, hard_cap=80):
    monkeypatch.setattr(driver, "HARD_CAP", hard_cap)
    monkeypatch.setattr(driver, "make_backend", lambda model, effort: backend)
    with pytest.raises(SystemExit) as exc:
        driver.main()
    return exc.value.code


def read_results(logs: Path) -> dict:
    files = list(logs.glob("mini_sre_results_*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def read_transcript(logs: Path) -> list[dict]:
    return [json.loads(line) for line in (logs / "mini_sre_transcript.jsonl").read_text().splitlines()]


def test_instance_text():
    text = mini.instance_text(APP)
    assert text == mini.build_task_text(APP).rstrip() + mini.CURL_INSTANCE_NOTE + mini.WORKFLOW_TEXT
    assert "executed in a new subshell" in text and "## How to work" in text
    assert "Important Rules" not in text and MARKER not in text
    assert "exactly ONE bash code block" in mini.system_text()


def test_workflow_names_no_objects_or_faults():
    # the rules must not name objects, faults or anything the judge checks
    for banned in ("kubectl", "Kubernetes", "pod", "CronJob", "Service", "namespace", "root cause", "affected"):
        assert banned.lower() not in mini.WORKFLOW_TEXT.lower(), banned


def test_parse_action_requires_exactly_one_block():
    assert mini.parse_action(block("kubectl get pods")) == ("kubectl get pods", 1)
    assert mini.parse_action("no block here") == (None, 0)
    assert mini.parse_action(block("a") + block("b")) == (None, 2)
    assert "found 2 actions" in mini.format_error_text(2)


def test_observation_and_timeout_texts():
    short = mini.observation_text(0, "hello")
    assert short == "<returncode>0</returncode>\n<output>\nhello\n</output>"
    long = mini.observation_text(1, "x" * 12000)
    assert (
        "<warning>" in long and "<output_head>" in long and "2000 characters elided" in long and "<output_tail>" in long
    )
    assert "timed out and has been killed" in mini.timeout_text("sleep 99", "")


def test_budget_notice():
    early = mini.budget_notice(used=3, cap=80, seconds_left=1400)
    assert early == "\n<budget>command 3 of 80, about 23 minutes left in this stage</budget>"
    near_cap = mini.budget_notice(used=70, cap=80, seconds_left=1400)
    assert "10 commands left in this stage" in near_cap and "submit your best answer now" in near_cap
    near_time = mini.budget_notice(used=3, cap=80, seconds_left=120)
    assert "about 2 minutes left" in near_time and "as the task instruction describes" in near_time
    both = mini.budget_notice(used=79, cap=80, seconds_left=30)
    assert "1 command and about 0 minutes left" in both


def test_accepted_submission():
    assert mini.accepted_submission('{"status":"200","message":"Submission received","stage":"diagnosis"}', "diagnosis")
    assert not mini.accepted_submission(
        '{"status":"200","message":"Submission received","stage":"mitigation"}', "diagnosis"
    )
    assert not mini.accepted_submission('{"detail":"already being evaluated."}', "diagnosis")


def test_model_submits_both_stages(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeBackend(
        [block("kubectl get pods"), block(CURL_DIAG), block("kubectl patch svc frontend -p '{}'"), block(CURL_FIX)]
    )
    assert run_main(monkeypatch, backend) == 0
    assert len(harness["commands"]) == 4
    first = backend.calls[0]
    assert first[0]["content"] == mini.system_text() and first[1]["content"] == mini.instance_text(APP)
    second = backend.calls[1]
    assert second[-1]["role"] == "user" and second[-1]["content"].startswith(
        "<returncode>0</returncode>\n<output>\npod-a Running\nwarn"
    )
    assert second[-2]["reasoning_content"] == "thinking"
    # no message is sent when the stage changes. The last message is the curl output.
    third = backend.calls[2]
    assert third[: len(second)] == second
    assert "Submission received" in third[-1]["content"] and "Current stage: mitigation" not in third[-1]["content"]
    results = read_results(harness["logs"])
    assert results["success"] is True and results["mini_sre"]["protocol"] == "mini"
    assert results["mini_sre"]["submit_mode"] == "curl"
    assert results["mini_sre"]["submitted_stages"] == ["diagnosis", "mitigation"]
    assert results["mini_sre"]["stages"]["diagnosis"]["termination_reason"] == "submitted_by_command"
    assert results["mini_sre"]["stages"]["mitigation"]["termination_reason"] == "submitted_by_command"
    assert results["mini_sre"]["stages"]["diagnosis"]["commands_used"] == 2  # the curl command counts
    assert results["usage_metrics"]["input_tokens"] == 400
    records = read_transcript(harness["logs"])
    assert [r["type"] for r in records[:2]] == ["meta", "prompt"]
    assert records[1]["messages"] == first  # the prompt as sent
    assert [r["by"] for r in records if r["type"] == "submit"] == ["command", "command"]
    assert [r["index"] for r in records if r["type"] == "command"] == [1, 2, 3, 4]
    assert [r["call"] for r in records if r["type"] == "model_call"] == [1, 2, 3, 4]
    assert (harness["logs"] / "steps" / "step_04" / "messages.json").exists()


def test_stage_ends_on_submission_receipt(monkeypatch, harness):
    """The stage ends when the conductor accepts the submission, before the stage changes."""
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    harness["conductor_evaluates_slowly"] = True
    backend = FakeBackend([block("kubectl get pods"), block(CURL_DIAG), block("kubectl get svc"), block(CURL_FIX)])
    assert run_main(monkeypatch, backend) == 0
    assert len(harness["commands"]) == 4  # one look and one submission per stage
    assert read_results(harness["logs"])["mini_sre"]["submitted_stages"] == ["diagnosis", "mitigation"]


def test_stage_change_counts_as_submission(monkeypatch, harness):
    backend = FakeBackend([block("python3 -c 'import requests'")])
    monkeypatch.setattr(driver, "conductor_status", lambda: {"stage": "tearing_down"})
    assert run_main(monkeypatch, backend) == 0
    assert read_results(harness["logs"])["mini_sre"]["termination_reason"] == "submitted_by_command"


def test_marker_line_is_an_ordinary_command(monkeypatch, harness):
    backend = FakeBackend([block(f"echo {MARKER}"), block(CURL_DIAG)])
    assert run_main(monkeypatch, backend) == 0
    assert harness["commands"][0] == f"echo {MARKER}"
    assert read_results(harness["logs"])["mini_sre"]["commands_used"] == 2


def test_three_format_errors_end_the_stage(monkeypatch, harness):
    backend = FakeBackend(["no block", block("a") + block("b"), "still none"])
    assert run_main(monkeypatch, backend) == driver.EXIT_NO_SUBMISSION
    assert harness["commands"] == []
    assert backend.calls[1][-1]["content"].startswith(
        "Please always provide EXACTLY ONE action in triple backticks, found 0 actions."
    )
    assert "found 2 actions" in backend.calls[2][-1]["content"]
    results = read_results(harness["logs"])
    assert results["mini_sre"]["termination_reason"] == "repeated_format_error" and results["success"] is False


def test_format_error_counter_resets_after_a_good_action(monkeypatch, harness):
    backend = FakeBackend(["none", block("kubectl get pods"), "none", "none", block(CURL_DIAG)])
    assert run_main(monkeypatch, backend) == 0
    assert harness["commands"] == ["kubectl get pods", CURL_DIAG]


def test_budget_line_after_every_command(monkeypatch, harness):
    backend = FakeBackend([block("a"), block("b"), block(CURL_DIAG)])
    assert run_main(monkeypatch, backend, hard_cap=80) == 0
    assert "<budget>command 1 of 80" in backend.calls[1][-1]["content"]
    assert "<budget>command 2 of 80" in backend.calls[2][-1]["content"]


def test_hard_cap_asks_for_submission(monkeypatch, harness):
    backend = FakeBackend([block("a"), block("b"), block(CURL_DIAG)])
    assert run_main(monkeypatch, backend, hard_cap=2) == 0
    notice = backend.calls[2][-1]["content"]
    assert notice.startswith("You have reached the limit of 2 commands for this stage.")
    assert "exactly as the task instruction describes" in notice and MARKER not in notice
    wrap_up = [r for r in read_transcript(harness["logs"]) if r["type"] == "wrap_up"]
    assert [(r["reason"], r["commands"]) for r in wrap_up] == [("hard_cap", 2)]
    assert read_results(harness["logs"])["mini_sre"]["termination_reason"] == "submitted_by_command"


def test_stage_ends_if_wrap_up_is_ignored(monkeypatch, harness):
    backend = FakeBackend([block(c) for c in "abcdef"])
    monkeypatch.setattr(driver, "WRAP_UP_CALLS", 2)
    assert run_main(monkeypatch, backend, hard_cap=2) == driver.EXIT_NO_SUBMISSION
    assert harness["commands"] == ["a", "b", "c", "d"]
    assert len(backend.calls) == 4  # two before the cap, two after the notice
    assert read_results(harness["logs"])["mini_sre"]["termination_reason"] == "hard_cap_no_submission"


def test_timeout_uses_the_timeout_template(monkeypatch, harness):
    backend = FakeBackend([block("sleep 99"), block(CURL_DIAG)])
    assert run_main(monkeypatch, backend) == 0
    assert backend.calls[1][-1]["content"].startswith(
        "The last command <command>sleep 99</command> timed out and has been killed."
    )
    assert read_results(harness["logs"])["mini_sre"]["commands_used"] == 2


def test_two_failed_model_calls_end_the_stage(monkeypatch, harness):
    backend = FakeBackend([Reply(error="model call failed: boom"), Reply(error="model call failed: boom")])
    assert run_main(monkeypatch, backend) == driver.EXIT_MODEL_FAILURE
    assert read_results(harness["logs"])["mini_sre"]["termination_reason"] == "model_failure"


def test_failed_mitigation_keeps_diagnosis(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeBackend([block(CURL_DIAG), "none", "none", "none"])
    assert run_main(monkeypatch, backend) == driver.EXIT_NO_SUBMISSION
    results = read_results(harness["logs"])
    assert results["mini_sre"]["submitted_stages"] == ["diagnosis"]
    assert results["mini_sre"]["stages"]["mitigation"]["termination_reason"] == "repeated_format_error"


def test_attempt_starting_at_mitigation(monkeypatch, harness):
    harness["stages"] = ["mitigation", "done"]
    harness["current"] = "mitigation"
    backend = FakeBackend([block(CURL_FIX)])
    assert run_main(monkeypatch, backend) == 0
    assert backend.calls[0][-1]["content"] == mini.instance_text(APP)
    assert "diagnosis" not in read_results(harness["logs"])["mini_sre"]["stages"]


def test_missing_model_is_infra_error(monkeypatch, harness):
    monkeypatch.setattr(driver, "MODEL", "")
    assert run_main(monkeypatch, FakeBackend([])) == driver.EXIT_INFRA


def test_no_real_problem_id_reaches_the_logs(monkeypatch, harness):
    monkeypatch.setenv("SREGYM_ARTIFACT_ID", "anon_test")
    backend = FakeBackend([block("kubectl get pods"), block(CURL_DIAG)])
    assert run_main(monkeypatch, backend) == 0
    assert _find_token_hit(harness["logs"], REAL_PROBLEM_ID) is None
