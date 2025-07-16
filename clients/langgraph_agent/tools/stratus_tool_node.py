import asyncio
import logging

from langchain_core.tools import BaseTool
from langgraph.types import Command

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class StratusToolNode:
    """A node that runs the tools requested in the last AIMessage."""

    def __init__(self, sync_tools: list[BaseTool], async_tools: list[BaseTool]) -> None:
        self.sync_tools_by_name = {t.name: t for t in sync_tools}
        self.async_tools_by_name = {t.name: t for t in async_tools}

    def __call__(self, inputs: dict):
        if messages := inputs.get("messages", []):
            message = messages[-1]
        else:
            raise ValueError("No message found in input")

        to_update = dict()
        new_messages = []
        for tool_call in message.tool_calls:
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

        to_update["messages"] = new_messages
        return to_update
