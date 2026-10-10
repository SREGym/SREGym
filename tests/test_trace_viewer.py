"""Viewer contracts: recorded grades, complete trace access, and read-only file boundaries."""

import csv
import hashlib
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from sregym.viewer.app import PAGE_SIZE, create_app
from sregym.viewer.browse import campaign_groups, fault_groups
from sregym.viewer.catalog import Catalog, campaign_path

TRACE = {"run": "fault/run_1/trajectory.json"}


def trajectory(name="codex", steps=1, **extra):
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "shared-session",
        "agent": {"name": name, "version": "1.0", "model_name": "test-model"},
        "steps": steps
        if isinstance(steps, list)
        else [{"step_id": i, "source": "agent", "message": f"Message {i}"} for i in range(1, steps + 1)],
        **extra,
    }


def write_trace(root: Path, value=None, relative="fault/run_1/trajectory.json"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value or trajectory()), encoding="utf-8")
    return path


def write_results(path, attempt="1", diagnosis="True", mitigation="False", status="complete", problem="fault", **extra):
    row = {
        "problem_id": problem,
        "attempt": attempt,
        "Diagnosis.success": diagnosis,
        "Mitigation.success": mitigation,
        "run_status": status,
        **extra,
    }
    with (path.parent / f"{problem}_results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


@pytest.fixture
def client(tmp_path):
    write_trace(tmp_path)
    return TestClient(create_app(tmp_path))


def test_offline_html_no_javascript_and_read_only(client, tmp_path):
    path = tmp_path / "fault/run_1/trajectory.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    response = client.get("/")
    assert response.status_code == 200
    assert "<script" not in response.text.lower()
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/static/viewer.css").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.post("/").status_code == 405
    assert "Unknown" in response.text
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_grades_join_exact_attempt_and_refresh(tmp_path):
    metadata = {"extra": {"sregym": {"problem_id": "fault", "run": 1, "submitted": True}}}
    first = write_trace(tmp_path, trajectory(**metadata))
    write_results(first)
    second = write_trace(
        tmp_path, trajectory(extra={"sregym": {"problem_id": "fault", "run": 2}}), "fault/run_2/trajectory.json"
    )
    write_results(second, attempt="2", diagnosis="False", mitigation="True")
    app = create_app(tmp_path)
    client = TestClient(app)
    client.get("/")
    runs = app.state.catalog.runs()
    assert [(r.diagnosis, r.mitigation) for r in runs] == [("Pass", "Fail"), ("Fail", "Pass")]
    write_results(first, mitigation="", status="incomplete")
    response = client.get("/", params={"run": "fault/run_1/trajectory.json", "tab": "evaluation"})
    assert "This attempt is incomplete" in response.text
    assert app.state.catalog.runs()[0].mitigation == "Unknown"
    # A CSV with the wrong attempt cannot confer a grade.
    write_results(first, attempt="3", mitigation="True")
    assert app.state.catalog.runs()[0].mitigation == "Unknown"


def test_ambiguous_results_remain_unknown(tmp_path):
    path = write_trace(tmp_path, trajectory(extra={"sregym": {"problem_id": "fault", "run": 1}}))
    write_results(path)
    result = path.parent / "fault_results.csv"
    with result.open("a") as stream:
        stream.write("fault,1,True,True,complete\n")
    run = Catalog(tmp_path).runs()[0]
    assert run.diagnosis == "Unknown"
    assert "Multiple" in run.evaluation_note


def test_tool_outputs_match_call_ids_not_position(tmp_path):
    value = trajectory()
    value["steps"][0].update(
        tool_calls=[
            {"tool_call_id": "a", "function_name": "first_tool", "arguments": {"command": "kubectl"}},
            {"tool_call_id": "b", "function_name": "second_tool", "arguments": {}},
        ],
        observation={
            "results": [
                {"source_call_id": "b", "content": "SECOND_RESULT"},
                {"source_call_id": "a", "content": "FIRST_RESULT"},
                {"content": "UNLINKED_RESULT"},
            ]
        },
    )
    write_trace(tmp_path, value)
    page = TestClient(create_app(tmp_path)).get("/", params=TRACE).text
    first = page.index("first_tool</h3>")
    second = page.index("second_tool</h3>")
    assert first < page.index("FIRST_RESULT") < second < page.index("SECOND_RESULT")
    assert "Observation without a call ID" in page


def test_pagination_search_full_text_and_stage_boundary(tmp_path):
    value = trajectory(steps=PAGE_SIZE + 7, extra={"sregym": {"diagnosis_submitted_step": 2}})
    value["steps"][0]["message"] = "x" * 12000 + "END_MARKER"
    value["steps"][0]["reasoning_content"] = "hidden reasoning"
    write_trace(tmp_path, value)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE).text
    assert 'id="step-25"' in page and 'id="step-26"' not in page
    assert "Read the full text" in page
    assert "END_MARKER" not in page
    assert "hidden reasoning" in page
    full = client.get("/text", params={"run": "fault/run_1/trajectory.json", "field": "message"})
    assert "END_MARKER</pre>" in full.text
    assert full.headers["Content-Type"].startswith("text/html")
    assert "Back to step 1" in full.text
    assert 'id="step-32"' in client.get("/", params={**TRACE, "focus": "32"}).text
    result = client.get("/", params={**TRACE, "search": "END_MARKER"}).text
    assert 'id="step-1"' in result and 'id="step-2"' not in result
    result = client.get("/", params={**TRACE, "stage": "After diagnosis submission"}).text
    assert 'id="step-1"' not in result and 'id="step-3"' in result


def test_subagents_use_trajectory_id_and_not_shared_session(tmp_path):
    child = trajectory("reviewer", trajectory_id="child")
    root = trajectory(subagent_trajectories=[child, trajectory("helper", trajectory_id="other")])
    root["steps"][0]["observation"] = {
        "results": [
            {
                "content": "delegated",
                "subagent_trajectory_ref": [{"trajectory_id": "child", "session_id": "shared-session"}],
            }
        ]
    }
    write_trace(tmp_path, root)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE).text
    assert "Open subagent: child" in page
    page = client.get("/", params={**TRACE, "doc": "0"}).text
    assert "Subagent: reviewer" in page and "Back to the parent trace" in page
    page = client.get("/", params={**TRACE, "doc": "-1"}).text
    assert "The subagent trajectory does not exist" in page


def test_valid_external_reference_and_continuation(tmp_path):
    write_trace(tmp_path, trajectory("worker"), "fault/run_1/child.json")
    root = trajectory(continued_trajectory_ref="child.json")
    write_trace(tmp_path, root)
    client = TestClient(create_app(tmp_path))
    assert "Open continuation" in client.get("/", params=TRACE).text
    page = client.get("/", params={"run": "fault/run_1/child.json", "campaign": "parent-campaign"}).text
    assert "worker" in page
    assert "parent-campaign</a>" in page


@pytest.mark.parametrize("version", [f"ATIF-v1.{i}" for i in range(8)])
def test_supported_schema_versions(tmp_path, version):
    write_trace(tmp_path, trajectory(schema_version=version))
    assert 'id="step-1"' in TestClient(create_app(tmp_path)).get("/", params=TRACE).text


@pytest.mark.parametrize(
    "malformed",
    [
        "{",
        json.dumps(trajectory(schema_version="ATIF-v99")),
        json.dumps(trajectory(steps=[{"step_id": 5, "source": "user", "message": "bad"}])),
    ],
)
def test_invalid_trace_is_visible_and_downloadable(tmp_path, malformed):
    path = write_trace(tmp_path)
    path.write_text(malformed)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE)
    assert page.status_code == 200
    assert "This trace could not be displayed" in page.text
    assert client.get("/download", params={"run": "fault/run_1/trajectory.json"}).text == malformed


def test_escape_untrusted_content_and_no_outside_reads(tmp_path):
    value = trajectory()
    value["steps"][0]["message"] = '<script>alert("x")</script><img src="https://evil.test">'
    value["continued_trajectory_ref"] = "../../../secret.json"
    path = write_trace(tmp_path, value)
    outside = tmp_path.parent / "viewer-secret.json"
    outside.write_text("SECRET_TOKEN")
    (path.parent / "secret.json").symlink_to(outside)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE).text
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert "SECRET_TOKEN" not in page
    for key in ["../viewer-secret.json", "fault/run_1/secret.json", "/etc/passwd"]:
        assert client.get("/download", params={"run": key}).status_code == 404
    path.unlink()
    path.symlink_to(outside)
    assert client.get("/download", params={"run": "fault/run_1/trajectory.json"}).status_code == 404


def test_reference_cannot_expose_arbitrary_json(tmp_path):
    write_trace(tmp_path, {"tokens": {"access_token": "SECRET"}}, "fault/run_1/auth.json")
    write_trace(tmp_path, trajectory(continued_trajectory_ref="auth.json"))
    client = TestClient(create_app(tmp_path))
    assert "Unavailable continuation" in client.get("/", params=TRACE).text
    assert client.get("/download", params={"run": "fault/run_1/auth.json"}).status_code == 404


def test_image_reference_is_local_and_correct_type(tmp_path):
    image_dir = tmp_path / "fault/run_1"
    value = trajectory()
    value["steps"][0]["message"] = [
        {"type": "image", "source": {"media_type": "image/png", "path": "picture.png"}},
        {"type": "image", "source": {"media_type": "image/png", "path": "https://remote.test/image"}},
    ]
    write_trace(tmp_path, value)
    (image_dir / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"data")
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE).text
    assert "External image not loaded" in page
    params = {"run": "fault/run_1/trajectory.json", "doc": "", "step": "1", "field": "message", "part": "0"}
    assert client.get("/image", params=params).status_code == 200
    (image_dir / "picture.png").write_text("SECRET")
    assert client.get("/image", params=params).status_code == 415


def test_single_arbitrarily_named_file_empty_directory_and_refresh(tmp_path):
    path = write_trace(tmp_path, relative="my-trace.json")
    client = TestClient(create_app(path))
    assert 'id="step-1"' in client.get("/").text
    value = trajectory()
    value["steps"][0]["message"] = "UPDATED"
    path.write_text(json.dumps(value))
    assert "UPDATED" in client.get("/").text
    directory_client = TestClient(create_app(tmp_path))
    assert "No trajectories found" in directory_client.get("/").text
    write_trace(tmp_path)
    assert 'id="step-1"' in directory_client.get("/", params=TRACE).text


def test_recorded_zero_and_cache_inclusion(tmp_path):
    write_trace(
        tmp_path,
        trajectory(
            final_metrics={
                "total_prompt_tokens": 0,
                "total_cached_tokens": 0,
                "total_completion_tokens": 0,
                "total_cost_usd": 0,
            }
        ),
    )
    page = TestClient(create_app(tmp_path)).get("/", params=TRACE).text
    assert "Cached input" in page and "(included)" in page
    assert "$0.0000" in page
    assert "<dd>0</dd>" in page


def test_generic_trace_does_not_borrow_adjacent_grades(tmp_path):
    path = write_trace(tmp_path)
    write_results(path, mitigation="True")
    assert Catalog(tmp_path).runs()[0].mitigation == "Unknown"


def test_generated_reader_links_pin_selection_and_jump_to_anchor(client):
    page = client.get("/", params=TRACE).text
    links = [parse_qs(urlsplit(unescape(link)).query) for link in re.findall(r'href="([^"]+)"', page)]
    evaluation = next(link for link in links if link.get("tab") == ["evaluation"])
    assert evaluation["run"] == [TRACE["run"]]
    response = client.get("/jump", params={"run": "fault/run_1/trajectory.json", "focus": 1}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("#step-1")


def test_campaign_pagination_does_not_open_a_trace(tmp_path):
    for i in range(PAGE_SIZE + 3):
        write_trace(tmp_path, relative=f"campaign_{i}/codex/fault/run_1/trajectory.json")
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params={"run_page": "2"}).text
    assert "Page 2 of 2" in page
    assert 'id="step-1"' not in page
    assert "Campaign directory" in page


def test_full_text_escapes_markup(client, tmp_path):
    value = trajectory()
    value["steps"][0]["message"] = '<script>alert("x")</script>' + "x" * 10000
    write_trace(tmp_path, value)
    client.get("/")
    page = client.get("/text", params={"run": "fault/run_1/trajectory.json"}).text
    assert "&lt;script&gt;" in page and "<script>" not in page


def test_jump_clears_filters_that_could_hide_destination(client):
    response = client.get(
        "/jump",
        params={"run": "fault/run_1/trajectory.json", "focus": 1, "search": "missing", "role": "user", "q": "fault"},
        follow_redirects=False,
    )
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert "search" not in params and "role" not in params
    assert params["q"] == ["fault"]
    assert 'id="step-1"' in client.get(response.headers["location"]).text


def test_search_opens_matching_reasoning_and_full_text_preserves_filters(tmp_path):
    value = trajectory()
    value["steps"][0]["reasoning_content"] = "MATCH" + "x" * 10000
    write_trace(tmp_path, value)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params={**TRACE, "search": "MATCH", "agent": "codex", "q": "fault"}).text
    assert '<details class="reasoning" open>' in page
    assert "Search match in reasoning" in page
    assert "search=MATCH" in page
    full = client.get(
        "/text",
        params={"run": "fault/run_1/trajectory.json", "field": "reasoning", "search": "MATCH", "agent": "codex"},
    ).text
    assert "search=MATCH" in full and "agent=codex" in full


def test_nested_campaigns_are_not_conflated():
    assert (
        campaign_path("batch/sol-max/date/codex/fault/run_1/trajectory.json", "fault", "codex") == "batch/sol-max/date"
    )
    assert (
        campaign_path("batch/sol-medium/date/codex/fault/run_1/trajectory.json", "fault", "codex")
        == "batch/sol-medium/date"
    )
    assert campaign_path("codex/fault/run_1/trajectory.json", "fault", "codex") == "."
    assert campaign_path("my-trace.json", "", "worker") == "."


def test_campaign_to_fault_to_attempt_navigation(tmp_path):
    for campaign in ("batch/sol-max/date", "batch/sol-medium/date"):
        for attempt in (1, 2, 3):
            path = write_trace(
                tmp_path,
                trajectory(extra={"sregym": {"problem_id": "fault", "run": attempt}}),
                f"{campaign}/codex/fault/run_{attempt}/trajectory.json",
            )
            write_results(path, attempt=str(attempt), mitigation="True" if attempt == 2 else "False")
    app = create_app(tmp_path)
    client = TestClient(app)
    home = client.get("/").text
    assert "2 campaigns · 6 recorded attempts" in home
    assert 'id="step-1"' not in home
    assert home.count('class="row-title campaign-title"') == 2
    runs = app.state.catalog.runs()
    assert len(campaign_groups(runs)) == 2
    campaign = "batch/sol-medium/date"
    faults = client.get("/", params={"campaign": campaign}).text
    assert "1 fault/model groups · 3 matching attempts" in faults
    assert faults.count('class="attempt-link ') == 3
    assert "sol-max" not in faults
    key = f"{campaign}/codex/fault/run_2/trajectory.json"
    reader = client.get("/", params={"run": key}).text
    assert 'id="step-1"' in reader
    assert "Step navigation" in reader and "Recorded attempts" not in reader
    assert "Attempt 2 · Pass" in reader
    assert "campaign=batch%2Fsol-medium%2Fdate" in reader
    assert "sol-max" not in reader


def test_grouping_separates_models_and_sorts_attempts_numerically(tmp_path):
    for attempt, model in ((10, "first"), (2, "first"), (1, "second")):
        value = trajectory(extra={"sregym": {"problem_id": "fault", "run": attempt}})
        value["agent"]["model_name"] = model
        write_trace(tmp_path, value, f"date/codex/fault/run_{attempt}/trajectory.json")
    groups = fault_groups(Catalog(tmp_path).runs())
    assert len(groups) == 2
    assert [run.attempt for run in groups[0]["runs"]] == ["2", "10"]


def test_browse_filters_counts_unknown_and_incomplete(tmp_path):
    for attempt, mitigation, status in ((1, "True", "complete"), (2, "False", "complete"), (3, "", "incomplete")):
        path = write_trace(
            tmp_path,
            trajectory(extra={"sregym": {"problem_id": "fault", "run": attempt}}),
            f"date/codex/fault/run_{attempt}/trajectory.json",
        )
        write_results(path, attempt=str(attempt), mitigation=mitigation, status=status)
    app = create_app(tmp_path)
    client = TestClient(app)
    home = client.get("/").text
    assert "1 pass" in home and "1 fail" in home and "1 unknown" in home
    assert 'class="result-bar"' in home and 'width="33.333333333333336"' in home
    assert '<span class="row-notice incomplete">1 incomplete</span>' in home
    page = client.get("/", params={"campaign": "date", "mitigation": "Fail"}).text
    assert "1 matching attempts" in page and "2 · Fail" in page and "3 · Unknown" not in page
    assert "Passed <strong>1</strong>" in page and "Failed <strong>1</strong>" in page
    assert "0 pass</a>" not in page
    page = client.get("/", params={"campaign": "date", "status": "incomplete"}).text
    assert "3 · Unknown · Incomplete" in page and "2 · Fail" not in page
    assert 'class="attempt-link unknown incomplete"' in page
    assert 'class="extra-filters" open' in page


def test_recent_campaigns_use_recorded_time_not_file_copy_time(tmp_path):
    for campaign, timestamp in (("aaa", "2026-10-08T12:00:00Z"), ("zzz", "2026-10-01T12:00:00Z")):
        value = trajectory(extra={"sregym": {"problem_id": "fault", "run": 1}})
        value["steps"][0]["timestamp"] = timestamp
        write_trace(tmp_path, value, f"{campaign}/codex/fault/run_1/trajectory.json")
    assert [group["key"] for group in campaign_groups(Catalog(tmp_path).runs())] == ["aaa", "zzz"]


def test_reader_prioritizes_actions_and_can_show_tools_only(tmp_path):
    value = trajectory(steps=3)
    value["steps"][1].update(
        tool_calls=[{"tool_call_id": "a", "function_name": "Bash", "arguments": {"command": "kubectl get pods"}}],
        observation={"results": [{"source_call_id": "a", "content": "TOOL_OUTPUT"}]},
    )
    write_trace(tmp_path, value)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params={**TRACE, "mode": "tools"}).text
    assert 'id="step-2"' in page and 'id="step-1"' not in page and 'id="step-3"' not in page
    assert '<div class="primary-payload">' in page
    assert '<p class="payload-label">Command</p>' in page
    assert '<details class="arguments" >' in page
    assert '<small class="outline-command" title="kubectl get pods">' in page
    assert '<details class="tool-output" >' in page
    assert "kubectl get pods" in page
    page = client.get("/", params={**TRACE, "search": "TOOL_OUTPUT"}).text
    assert '<details class="tool-output" open>' in page


def test_result_drilldown_and_filter_recovery_keep_exact_scope(tmp_path):
    for fault in ("fault", "fault_extended"):
        path = write_trace(
            tmp_path,
            trajectory(extra={"sregym": {"problem_id": fault, "run": 1}}),
            f"date/codex/{fault}/run_1/trajectory.json",
        )
        write_results(path, problem=fault)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params={"campaign": "date"}).text
    href = re.search(r'href="([^"]+)" aria-label="1 mitigation fail: fault"', page).group(1)
    response = client.get(unescape(href))
    assert "1 matching attempts" in response.text
    assert 'name="fault" value="fault"' in response.text
    assert "fault extended</a>" not in response.text
    assert "Remove fault filter: fault" in response.text
    empty = client.get("/", params={"campaign": "date", "fault": "fault", "q": "no-match"}).text
    href = re.search(r'href="([^"]+)">Clear current filters</a>', empty).group(1)
    assert parse_qs(urlsplit(unescape(href)).query) == {"campaign": ["date"]}
    assert "2 matching attempts" in client.get(unescape(href)).text


@pytest.mark.parametrize("key,label", [("command", "Command"), ("input", "Input"), ("options", None)])
def test_recorded_primary_payload_and_structured_fallback(tmp_path, key, label):
    value = trajectory()
    value["steps"][0]["tool_calls"] = [
        {"tool_call_id": "call", "function_name": "tool", "arguments": {key: "<tag>\nsecond line", "timeout": 42}}
    ]
    write_trace(tmp_path, value)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE).text
    assert "<tag>" not in page
    assert "&lt;tag&gt;" in page
    if label:
        assert f'<p class="payload-label">{label}</p>' in page
        assert '<details class="arguments" >' in page
        assert 'class="outline-command" title="&lt;tag&gt;"' in page
    else:
        assert '<div class="primary-payload">' not in page
        assert '<details class="arguments" open>' in page
    search = client.get("/", params={**TRACE, "search": "42"}).text
    assert '<details class="arguments" open>' in search


def test_long_primary_payload_is_bounded_but_full_arguments_remain_available(tmp_path):
    value = trajectory()
    command = "x" * 10000 + "TAIL_NOT_PREVIEW"
    value["steps"][0]["tool_calls"] = [
        {"tool_call_id": "call", "function_name": "Bash", "arguments": {"command": command}}
    ]
    write_trace(tmp_path, value)
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE).text
    assert "TAIL_NOT_PREVIEW" not in page
    assert "Preview ends after 8,000 characters" in page
    full = client.get("/text", params={**TRACE, "step": 1, "field": "arguments-0"}).text
    assert "TAIL_NOT_PREVIEW" in full
    assert client.get("/download", params=TRACE).json() == value


def test_recorded_failure_context_does_not_reclassify_incomplete_attempt(tmp_path):
    path = write_trace(tmp_path, trajectory(extra={"sregym": {"problem_id": "fault", "run": 1}}))
    write_results(path, **{"Mitigation.reason": "recorded_reason"})
    client = TestClient(create_app(tmp_path))
    page = client.get("/", params=TRACE).text
    assert "Recorded mitigation reason:</strong> recorded_reason" in page
    write_results(path, mitigation="", status="incomplete", incomplete_reason="grading did not finish")
    page = client.get("/", params=TRACE).text
    assert "Recorded incomplete-attempt reason:</strong> grading did not finish" in page
    assert 'class="badge unknown">Unknown' in page
    assert 'class="badge fail">Fail' not in page
