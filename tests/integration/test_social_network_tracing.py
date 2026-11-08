"""Trace propagation checks against a deployed Social Network and central Jaeger.

Set SOCIAL_NETWORK_URL and JAEGER_URL to owned port-forwards, then run this
module with pytest -m integration. The tests create uniquely named users/posts.
"""

import os
import time
import uuid

import pytest
import requests

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def endpoints():
    frontend = os.environ.get("SOCIAL_NETWORK_URL")
    jaeger = os.environ.get("JAEGER_URL")
    if not frontend or not jaeger:
        pytest.skip("requires SOCIAL_NETWORK_URL and JAEGER_URL")
    return frontend.rstrip("/"), jaeger.rstrip("/")


def _traces(jaeger, trace_id):
    response = requests.get(f"{jaeger}/api/traces/{trace_id}", timeout=15)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return response.json().get("data") or []


@pytest.fixture(scope="module")
def users(endpoints):
    frontend, _ = endpoints
    suffix = uuid.uuid4().hex[:10]
    result = []
    for index in range(2):
        user = {"username": f"trace_{suffix}_{index}", "user_id": 800_000_000 + uuid.uuid4().int % 100_000_000}
        response = requests.post(
            f"{frontend}/wrk2-api/user/register",
            data={**user, "first_name": "Trace", "last_name": "Check", "password": "trace-check"},
            timeout=30,
        )
        assert response.status_code == 200 and "Success!" in response.text, response.text
        result.append(user)
    # Match the populated social graph used by the benchmark workload.
    response = requests.post(
        f"{frontend}/wrk2-api/user/follow",
        data={"user_id": result[1]["user_id"], "followee_id": result[0]["user_id"]},
        timeout=30,
    )
    assert response.status_code == 200 and "Success!" in response.text, response.text
    return result


@pytest.mark.parametrize("sampled", [True, False])
def test_compose_preserves_trace_and_sampling(endpoints, users, sampled, record_property):
    frontend, jaeger = endpoints
    trace_id = uuid.uuid4().hex
    external_parent = "1234567890abcdef"
    record_property("trace_id", trace_id)
    response = requests.post(
        f"{frontend}/wrk2-api/post/compose",
        headers={"uber-trace-id": f"{trace_id}:{external_parent}:0:{int(sampled)}"},
        data={
            **users[0],
            "post_type": 0,
            "text": f"trace-check {trace_id} @{users[1]['username']} https://example.com/{trace_id}",
            "media_ids": "[1]",
            "media_types": '["png"]',
        },
        timeout=30,
    )
    assert response.status_code == 200 and "Successfully upload post" in response.text, response.text

    required = {
        "nginx-web-server",
        "compose-post-service",
        "user-service",
        "text-service",
        "url-shorten-service",
        "user-mention-service",
        "media-service",
        "unique-id-service",
        "post-storage-service",
        "user-timeline-service",
        "home-timeline-service",
    }
    deadline = time.monotonic() + 45
    traces = []
    while time.monotonic() < deadline:
        traces = _traces(jaeger, trace_id)
        if not sampled:
            assert not traces, "An unsampled request produced backend spans"
        elif traces:
            trace = traces[0]
            spans = trace["spans"]
            services = {trace["processes"][span["processID"]]["serviceName"] for span in spans}
            span_ids = {span["spanID"] for span in spans}
            missing_parents = {
                reference["spanID"]
                for span in spans
                for reference in span.get("references", [])
                if reference["refType"] == "CHILD_OF" and reference["spanID"] not in span_ids | {external_parent}
            }
            if required <= services and not missing_parents:
                return
        time.sleep(2)
    if sampled:
        assert traces, "The sampled request was not exported"
        assert required <= services, f"Missing services: {required - services}"
        assert not missing_parents, f"Unexported parent spans: {missing_parents}"


@pytest.mark.parametrize("explicit_id", [True, False])
def test_registration_preserves_social_graph_parent(endpoints, explicit_id, record_property):
    frontend, jaeger = endpoints
    trace_id = uuid.uuid4().hex
    record_property("trace_id", trace_id)
    data = {"first_name": "Trace", "last_name": "Check", "username": f"trace_{trace_id}", "password": "trace-check"}
    if explicit_id:
        data["user_id"] = 800_000_000 + uuid.uuid4().int % 100_000_000
    response = requests.post(
        f"{frontend}/{'wrk2-api' if explicit_id else 'api'}/user/register",
        headers={"uber-trace-id": f"{trace_id}:1234567890abcdef:0:1"},
        data=data,
        allow_redirects=False,
        timeout=30,
    )
    assert response.status_code in {200, 302}, response.text
    operation = "register_user_withid_server" if explicit_id else "register_user_server"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        traces = _traces(jaeger, trace_id)
        if traces:
            spans = traces[0]["spans"]
            parents = {span["spanID"] for span in spans if span["operationName"] == operation}
            children = [span for span in spans if span["operationName"] == "insert_user_server"]
            if parents and children:
                assert all(
                    any(ref["refType"] == "CHILD_OF" and ref["spanID"] in parents for ref in span["references"])
                    for span in children
                ), "Social Graph's parent is not the User service's exported span"
                return
        time.sleep(2)
    pytest.fail("Registration did not export both User and Social Graph spans in the same trace")


def test_login_and_timeline_contents(endpoints, users):
    frontend, _ = endpoints
    marker = f"trace-content-{uuid.uuid4().hex}"
    with requests.Session() as session:
        response = session.post(
            f"{frontend}/api/user/login",
            data={"username": users[0]["username"], "password": "trace-check"},
            allow_redirects=False,
            timeout=30,
        )
        assert response.status_code == 302 and "login_token" in session.cookies, response.text
        response = session.post(f"{frontend}/api/post/compose", data={"post_type": 0, "text": marker}, timeout=30)
        assert response.status_code == 200 and "Successfully upload post" in response.text, response.text
    for kind, user in [("user", users[0]), ("home", users[1])]:
        response = requests.get(
            f"{frontend}/wrk2-api/{kind}-timeline/read",
            params={"user_id": user["user_id"], "start": 0, "stop": 10},
            timeout=30,
        )
        response.raise_for_status()
        assert any(post["text"] == marker for post in response.json()), response.text


def test_wrong_password_does_not_log_in(endpoints, users):
    frontend, _ = endpoints
    response = requests.post(
        f"{frontend}/api/user/login",
        data={"username": users[0]["username"], "password": "wrong-password"},
        allow_redirects=False,
        timeout=30,
    )
    assert "login_token" not in response.cookies
    assert "User login failure" in response.text, response.text
