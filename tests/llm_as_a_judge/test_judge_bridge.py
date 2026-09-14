import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest
from openai import APIError

from clients.cursor.judge_bridge import make_handler as legacy_make_handler
from llm_backend import judge_bridge
from llm_backend.init_backend import get_llm_backend_for_judge


@pytest.fixture
def bridge_server(monkeypatch):
    requests = []

    def run(prompt, model, backend):
        requests.append((prompt, model, backend))
        return '{"judgment":"True","reasoning":"Matches the supplied cause"}'

    monkeypatch.setattr(judge_bridge, "_run_agent", run)
    with ThreadingHTTPServer(("127.0.0.1", 0), legacy_make_handler("auto")) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("model", ["gpt-5", "anthropic/claude-test"])
def test_existing_backend_roundtrip(bridge_server, monkeypatch, model):
    url, requests = bridge_server
    monkeypatch.setenv("SREGYM_JUDGE_BRIDGE_URL", f"{url}/v1")
    backend = get_llm_backend_for_judge(
        model_name=model, provider="anthropic", api_base="http://unused.invalid", api_key="unused"
    )
    result = backend.inference("Submitted diagnosis", system_prompt="Existing judge rubric")
    assert json.loads(result.content)["judgment"] == "True"
    assert result.usage_metadata is None
    assert "token_usage" not in result.response_metadata
    assert requests == [
        ("[system]\nExisting judge rubric\n\n[user]\nSubmitted diagnosis", model.rsplit("/", 1)[-1], "cursor")
    ]


def test_manual_cursor_response_and_health(bridge_server):
    url, requests = bridge_server
    with urlopen(f"{url}/health", timeout=2) as response:
        assert json.load(response) == {"status": "ok"}
    assert not requests
    body = json.dumps(
        {"model": "openai/auto", "messages": [{"role": "user", "content": [{"type": "text", "text": "Judge this"}]}]}
    ).encode()
    with urlopen(Request(f"{url}/v1/chat/completions", data=body), timeout=2) as response:
        payload = json.load(response)
    assert payload["choices"][0]["message"]["content"].startswith('{"judgment"')
    assert "usage" not in payload  # Unavailable counts must not be reported as measured zeros.
    assert requests == [("[user]\nJudge this", "auto", "cursor")]


@pytest.mark.parametrize("managed", [True, False])
def test_managed_cli_fails_once_while_api_retry_behavior_is_preserved(bridge_server, monkeypatch, managed):
    url, _ = bridge_server
    calls = []

    def fail_once(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("subscription limit reached")
        return "ok"

    monkeypatch.setattr(judge_bridge, "_run_agent", fail_once)
    monkeypatch.setenv("JUDGE_API_BASE", f"{url}/v1")
    monkeypatch.setenv("JUDGE_API_KEY", "dummy")
    if managed:
        monkeypatch.setenv("SREGYM_JUDGE_BRIDGE_URL", f"{url}/v1")
    else:
        monkeypatch.delenv("SREGYM_JUDGE_BRIDGE_URL", raising=False)
    backend = get_llm_backend_for_judge(model_name="gpt-5")
    if managed:
        with pytest.raises(APIError, match="subscription limit reached") as raised:
            backend.inference("Judge this")
        assert raised.value.status_code == 502
        assert len(calls) == 1
    else:
        assert backend.inference("Judge this").content == "ok"
        assert len(calls) == 2
