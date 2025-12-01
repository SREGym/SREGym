import asyncio
import logging

from langchain_core.tools import BaseTool

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class StatefulAsyncToolNode:
    """A node that runs the stateful remote mcp tools requested in the last AIMessage."""

    def __init__(self, node_tools: list[BaseTool]) -> None:
        self.tools_by_name = {t.name: t for t in node_tools}

    async def __call__(self, inputs: dict):
        if messages := inputs.get("messages", []):
            message = messages[-1]
        else:
            raise ValueError("No message found in input")
        logger.info(f"StatefulAsyncToolNode: {message}")
        outputs = []
        for tool_call in message.tool_calls:
            logger.info(f"invoking tool: {tool_call['name']}, tool_call: {tool_call}")
            tool_result = await self.tools_by_name[tool_call["name"]].ainvoke(
                {
                    "type": "tool_call",
                    "name": tool_call["name"],
                    "args": {"state": inputs, **tool_call["args"]},
                    "id": tool_call["id"],
                }
            )
            logger.info(f"tool_result: {tool_result}")
            outputs += tool_result.update["messages"]

        return {"messages": outputs}
