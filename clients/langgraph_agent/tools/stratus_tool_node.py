import asyncio
import logging

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic_core import ValidationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def reschedule_tool_calls(tool_calls):
    # reschedule the order of tool_calls
    rescheduled_tool_calls = []
    submit_tool_call = []
    wait_tool_call = []
    for tool_call in tool_calls:
        if tool_call['name'] == "submit_tool":
            submit_tool_call.append(tool_call)
        elif tool_call['name'] == "wait_tool":
            wait_tool_call.append(tool_call)
        else:
            rescheduled_tool_calls.append(tool_call)
    # submit_tool call is scheduled the first;
    # wait_tool call is scheduled the last.
    rescheduled_tool_calls = submit_tool_call + rescheduled_tool_calls + wait_tool_call
    return rescheduled_tool_calls


class StratusToolNode:
    """A node that runs the tools requested in the last AIMessage."""

    def __init__(self,
                 sync_tools: list[BaseTool],
                 async_tools: list[BaseTool],
                 max_tool_call_one_round) -> None:
        self.sync_tools_by_name = {t.name: t for t in sync_tools}
        self.async_tools_by_name = {t.name: t for t in async_tools}
        self.max_tool_call_one_round = max_tool_call_one_round

    def __call__(self, inputs: dict):
        if messages := inputs.get("messages", []):
            message = messages[-1]
        else:
            raise ValueError("No message found in input")

        rescheduled_tool_calls = reschedule_tool_calls(message.tool_calls)

        to_update = dict()
        new_messages = []
        for i, tool_call in enumerate(rescheduled_tool_calls):
            if i >= self.max_tool_call_one_round:
                message = f"Error: The maximum number of tool_calls allowed " \
                          f"for one round is {self.max_tool_call_one_round}."
                logger.info(f"Tool_call denied; tool_call: {tool_call}\n{message}")
                new_messages += [ToolMessage(
                    content=message,
                    tool_call_id=tool_call["id"]
                )]
                continue

            is_submit_tried = inputs["submit_tried"] or to_update.get("submit_tried", False)
            if is_submit_tried and tool_call['name'] != "submit_tool":
                message = f"Error: It is not allowed to call other tools after " \
                          f"calling the submit_tool for submission."
                logger.info(f"Tool_call denied; tool_call: {tool_call}\n{message}")
                new_messages += [ToolMessage(
                    content=message,
                    tool_call_id=tool_call["id"]
                )]
                continue

            if tool_call["name"] == "submit_tool":
                to_update["submit_tried"] = True

            try:
                logger.info(f"invoking tool: {tool_call['name']}, tool_call: {tool_call}")
                if tool_call["name"] in self.async_tools_by_name:
                    tool_result = asyncio.run(self.async_tools_by_name[tool_call["name"]].ainvoke(
                        {
                            "type": "tool_call",
                            "name": tool_call["name"],
                            "args": {"state": inputs, **tool_call["args"]},
                            "id": tool_call["id"],
                        }
                    ))
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
                    raise ValueError(f'Tool {tool_call["name"]} does not exist!')

                assert isinstance(tool_result, Command), \
                    f"Tool {tool_call['name']} should return a Command object, but return {type(tool_result)}"
                logger.info(f"tool_result: {tool_result}")
                new_messages += tool_result.update["messages"]
                to_update = {
                    **to_update,
                    **tool_result.update,  # this is the key part
                }
            except ValidationError as e:
                logger.error(f"tool_call: {tool_call}\nError: {e}")
                new_messages += [ToolMessage(
                    content=f"Error: {e}; This happens usually because you are "
                            f"passing inappropriate arguments to the tool.",
                    tool_call_id=tool_call["id"]
                )]

        to_update["messages"] = new_messages
        return to_update
