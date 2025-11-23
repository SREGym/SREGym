import asyncio
import logging

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class BasicToolNode:
    """A node that runs the tools requested in the last AIMessage."""

    def __init__(self, node_tools: list[BaseTool], is_async: bool) -> None:
        self.tools_by_name = {t.name: t for t in node_tools}
        self.is_async = is_async

    async def __call__(self, inputs: dict):
        if messages := inputs.get("messages", []):
            message = messages[-1]
        else:
            raise ValueError("No message found in input")
        logger.info(f"BasicToolNode: {message}")
        outputs = []
        for tool_call in message.tool_calls:
            # tool_call["args"]["tool_call_id"] = tool_call["id"]
            # tool_call["args"].pop("id", None)
            logger.info(f"invoking tool: {tool_call["name"]}, tool_call: {tool_call}")
            if self.is_async:
                tool_call["args"].update({"state": inputs})
                tool_result = await self.tools_by_name[tool_call["name"]].ainvoke(tool_call)
                tool_call["args"].pop("state", None)
            else:
                tool_result = self.tools_by_name[tool_call["name"]].invoke(tool_call["args"])
            logger.info(f"tool_result: {tool_result}")
            outputs.append(
                ToolMessage(
                    content=tool_result,
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"],
                )
            )
        return {"messages": outputs}
