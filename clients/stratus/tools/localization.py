import asyncio
import subprocess
from contextlib import AsyncExitStack
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from mcp import ClientSession
from mcp.client.sse import sse_client

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig

langgraph_tool_config = LanggraphToolConfig()

localization_tool_docstring = """
Use this tool to retrieve the UID of a specified resource.

    Args:
        resource_type (str): The type of the resource (e.g., 'pod', 'service', 'deployment').
        resource_name (str): The name of the resource.
        namespace (str): The namespace of the resource.
    Returns:
        str: The UID of the specified resource.
"""


@tool(description=localization_tool_docstring)
async def get_resource_uid(
    resource_type: str,
    resource_name: str,
    namespace: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    exit_stack = AsyncExitStack()
    server_url = langgraph_tool_config.submit_mcp_url
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))
    await session.initialize()
    result = await session.call_tool(
        "localization",
        arguments={
            "resource_type": resource_type,
            "resource_name": resource_name,
            "namespace": namespace,
        },
    )
    await exit_stack.aclose()
    uid = result.content[0].text
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=uid,
                    tool_call_id=tool_call_id,
                ),
            ]
        }
    )
