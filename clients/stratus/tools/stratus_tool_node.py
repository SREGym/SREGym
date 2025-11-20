import asyncio
import logging

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic_core import ValidationError

logger = logging.getLogger("all.stratus.tool_node")
logger.propagate = True
logger.setLevel(logging.DEBUG)


def reschedule_tool_calls(tool_calls):
    # reschedule the order of tool_calls
    rescheduled_tool_calls = []
    submit_tool_call = []
    wait_tool_call = []
    for tool_call in tool_calls:
        if tool_call["name"] == "submit_tool":
            submit_tool_call.append(tool_call)
        elif tool_call["name"] == "wait_tool":
            wait_tool_call.append(tool_call)
        else:
            rescheduled_tool_calls.append(tool_call)
    # submit_tool call is scheduled the first;
    # wait_tool call is scheduled the last.
    rescheduled_tool_calls = submit_tool_call + rescheduled_tool_calls + wait_tool_call
    return rescheduled_tool_calls


class StratusToolNode:
    """A node that runs the tools requested in the last AIMessage."""

    def __init__(self, sync_tools: list[BaseTool], async_tools: list[BaseTool]) -> None:
        self.sync_tools_by_name = {t.name: t for t in sync_tools} if sync_tools is not None else None
        self.async_tools_by_name = {t.name: t for t in async_tools} if async_tools is not None else None

    async def __call__(self, inputs: dict):
        if messages := inputs.get("messages", []):
            message = messages[-1]
        else:
            raise ValueError("No message found in input")

        if not isinstance(message, AIMessage):
            logger.warning(
                f"Expected last message to be an AIMessage, but got {type(message)}.\n" f"{inputs.get('messages', [])}"
            )
            raise ValueError("Last message is not an AIMessage; skipping tool invocation.")

        arena_logger = logging.getLogger("sregym-global")
        if message.content != "":
            arena_logger.info(f"[LLM] {message.content}")
            # logger.info(f"{message.content}")

        if not getattr(message, "tool_calls", None):
            logger.warning("AIMessage does not contain tool_calls.")
            return {"messages": []}

        if len(message.tool_calls) > 1:
            logger.warning("more than 1 tool call found. Calling in order", extra={"Tool Calls": message.tool_calls})
            logger.warning("technically, only one tool call allowed")

        to_update = dict()
        new_messages = []
        for i, tool_call in enumerate(message.tool_calls):
            try:
                # logger.info(f"[STRATUS_TOOLNODE] invoking tool: {tool_call['name']}, tool_call: {tool_call}")
                arg_list = [f"{key} = {value}" for key, value in tool_call["args"].items()]
                arena_logger.info(f"[LLM] Agent choose to call: {tool_call['name']}({', '.join(arg_list)})")
                logger.info(f"[STRATUS_TOOLNODE] Agent choose to call: {tool_call['name']}({', '.join(arg_list)})")
                if tool_call["name"] in self.async_tools_by_name:
                    tool_result = await self.async_tools_by_name[tool_call["name"]].ainvoke(
                        {
                            "type": "tool_call",
                            "name": tool_call["name"],
                            "args": {"state": inputs, **tool_call["args"]},
                            "id": tool_call["id"],
                        }
                    )
                elif tool_call["name"] in self.sync_tools_by_name:
                    tool_result = self.sync_tools_by_name[tool_call["name"]].invoke(
                        {
                            "type": "tool_call",
                            "name": tool_call["name"],
                            "args": {"state": inputs, **tool_call["args"]},
                            "id": tool_call["id"],
                        }
                    )
                else:
                    logger.info(f"agent tries to call tool that DNE: {tool_call['name']}")
                    Command(
                        update={
                            "messages": [
                                ToolMessage(
                                    content=f"Tool {tool_call['name']} does not exist!",
                                    tool_call_id=tool_call["id"],
                                )
                            ]
                        }
                    )

                assert isinstance(
                    tool_result, Command
                ), f"Tool {tool_call['name']} should return a Command object, but return {type(tool_result)}"
                logger.debug(f"[STRATUS_TOOLNODE] tool_result: {tool_result}")
                if tool_result.update["messages"]:
                    combined_content = "\n".join([message.content for message in tool_result.update["messages"]])
                    arena_logger.info(f"[ENV] Tool {tool_call['name']} returned: \n {combined_content}")
                new_messages += tool_result.update["messages"]
                to_update = {
                    **to_update,
                    **tool_result.update,  # this is the key part
                }
            except ValidationError as e:
                logger.error(f"tool_call: {tool_call}\nError: {e}")
                arena_logger.error(f"[ENV] Tool Call {tool_call['name']} failed: \n {e}")
                new_messages += [
                    ToolMessage(
                        content=f"Error: {e}; This happens usually because you are "
                        f"passing inappropriate arguments to the tool.",
                        tool_call_id=tool_call["id"],
                    )
                ]

        to_update["messages"] = new_messages
        return to_update
