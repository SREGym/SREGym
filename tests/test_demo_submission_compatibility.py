from clients.demo.driver import parse_submit_command, resolve_demo_submission_stage


def test_demo_parser_accepts_staged_submission():
    assert parse_submit_command('submit("diagnosis", "root cause")') == ("diagnosis", "root cause")
    assert parse_submit_command('submit("mitigation", "")') == ("mitigation", "")


def test_demo_parser_keeps_legacy_current_stage_submission():
    assert parse_submit_command('submit("root cause")') == (None, "root cause")
    assert parse_submit_command(r'submit("root cause: \"memory\"")') == (None, 'root cause: "memory"')


def test_demo_parser_does_not_treat_kubectl_as_submission():
    assert parse_submit_command("kubectl get pods") is None
    assert parse_submit_command('submit("unknown", "answer")') is None
    assert parse_submit_command("submit(answer)") is None
    assert parse_submit_command('submit([], "answer")') is None
    assert parse_submit_command('other("diagnosis", "answer")') is None


def test_second_legacy_demo_submission_targets_mitigation():
    assert resolve_demo_submission_stage(None, None) is None
    assert resolve_demo_submission_stage(None, "diagnosis") == "mitigation"
    assert resolve_demo_submission_stage("diagnosis", "diagnosis") == "diagnosis"
