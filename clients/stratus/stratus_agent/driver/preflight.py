import contextlib
import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from llm_backend.init_backend import get_llm_backend_for_agent

PREFLIGHT_TOOL_NAME = "preflight_echo"
PREFLIGHT_TOOL_VALUE = "ok"


@tool(PREFLIGHT_TOOL_NAME)
def preflight_echo(value: str) -> str:
    """Echo the provided value for validating model tool calling."""
    return value


def _tool_call_name(tool_call: Any) -> str | None:
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict) and function.get("name"):
            return function["name"]
        return tool_call.get("name")
    return getattr(tool_call, "name", None)


def _tool_call_args(tool_call: Any) -> dict[str, Any]:
    if isinstance(tool_call, dict):
        if isinstance(tool_call.get("args"), dict):
            return tool_call["args"]
        function = tool_call.get("function")
        if isinstance(function, dict):
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                return arguments
            if isinstance(arguments, str):
                with contextlib.suppress(json.JSONDecodeError):
                    parsed = json.loads(arguments)
                    if isinstance(parsed, dict):
                        return parsed
        return {}

    args = getattr(tool_call, "args", None)
    return args if isinstance(args, dict) else {}


def validate_preflight_tool_call(ai_message: Any) -> None:
    tool_calls = getattr(ai_message, "tool_calls", None)
    if not tool_calls:
        raise RuntimeError("model returned no tool calls during Stratus preflight")

    seen_tool_names = []
    for tool_call in tool_calls:
        name = _tool_call_name(tool_call)
        seen_tool_names.append(name or "<unknown>")
        if name != PREFLIGHT_TOOL_NAME:
            continue

        args = _tool_call_args(tool_call)
        value = args.get("value")
        if isinstance(value, str) and value.lower() == PREFLIGHT_TOOL_VALUE:
            return
        raise RuntimeError(
            f"model called {PREFLIGHT_TOOL_NAME!r}, but with invalid args {args!r}; "
            f"expected value={PREFLIGHT_TOOL_VALUE!r}"
        )

    raise RuntimeError(
        f"model returned tool calls {seen_tool_names!r}, but did not call required tool {PREFLIGHT_TOOL_NAME!r}"
    )


def run_stratus_preflight() -> None:
    backend = get_llm_backend_for_agent()
    backend.inference("say ok", system_prompt="Reply with exactly ok.")

    ai_message = backend.inference(
        messages=[
            SystemMessage(
                content=("This is a preflight check. You must call the provided tool; do not answer with normal text.")
            ),
            HumanMessage(content=f'Call {PREFLIGHT_TOOL_NAME} with value "{PREFLIGHT_TOOL_VALUE}".'),
        ],
        tools=[preflight_echo],
    )
    validate_preflight_tool_call(ai_message)
