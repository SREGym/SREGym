"""force_submit must hand the submit tool a plain string.

Reasoning models (Gemini 3.x, Claude with extended thinking) return
``AIMessage.content`` as a list of ``thinking``/``text`` blocks. The MCP
``submit`` tool validates ``ans: str``; passing the list raised a pydantic
ValidationError, which crashed the agent process and recorded ``no_submission``
(SREGym-Lite run of internal_traffic_policy_local_astronomy_shop).
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from clients.stratus.stratus_agent import base_agent
from clients.stratus.stratus_agent.base_agent import BaseAgent, message_text

_BLOCKS = [
    {"type": "thinking", "thinking": "Let me reason about this."},
    {"type": "text", "text": "The root cause is internalTrafficPolicy=Local on service/recommendation."},
]


def test_message_text_flattens_block_lists():
    assert message_text(_BLOCKS) == "The root cause is internalTrafficPolicy=Local on service/recommendation."
    assert message_text("  plain  ") == "plain"
    assert message_text(["a", {"type": "text", "text": "b"}]) == "a\nb"
    assert message_text(None) == ""


def _agent(responses):
    agent = object.__new__(BaseAgent)
    agent.max_step = 20
    agent.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    agent.submit_tool = SimpleNamespace(name="submit")
    agent.llm = SimpleNamespace(inference=lambda messages, tools=None: responses.pop(0))
    return agent


def test_force_submit_flattens_plain_text_fallback(monkeypatch):
    submitted = {}

    async def fake_submit(ans, *, stage):
        submitted["ans"] = ans
        submitted["stage"] = stage

    monkeypatch.setattr(base_agent, "manual_submit_tool", fake_submit)
    # First call: model answers without a tool call. Second call: block-list text.
    agent = _agent([AIMessage(content="I am not done yet."), AIMessage(content=_BLOCKS)])

    result = asyncio.run(agent.force_submit({"messages": []}))

    assert result["submitted"] is True
    assert submitted["stage"] == "diagnosis"
    assert isinstance(submitted["ans"], str)
    assert submitted["ans"] == "The root cause is internalTrafficPolicy=Local on service/recommendation."


def test_force_submit_uses_submit_tool_call_when_present(monkeypatch):
    submitted = {}

    async def fake_submit(ans, *, stage):
        submitted["ans"] = ans

    monkeypatch.setattr(base_agent, "manual_submit_tool", fake_submit)
    tool_call = {"name": "submit", "args": {"ans": "service/recommendation misconfigured"}, "id": "c1"}
    agent = _agent([AIMessage(content="", tool_calls=[tool_call])])

    asyncio.run(agent.force_submit({"messages": []}))

    assert submitted["ans"] == "service/recommendation misconfigured"
