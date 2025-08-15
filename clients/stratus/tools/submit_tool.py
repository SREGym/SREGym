import logging
from contextlib import AsyncExitStack
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from mcp import ClientSession
from mcp.client.sse import sse_client

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig

submit_tool_docstring = """
Use this tool to submit your answer to the assigned tasks. You can give partial answer or empty answer
    (still of type dict) if you can not solve all of them.

    Args:
        ans (string): the answer you would like to submit
"""
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

langgraph_tool_config = LanggraphToolConfig()


@tool(description=submit_tool_docstring)
async def submit_tool(ans: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    # makes http call to benchmark submission server
    logging.info(f"submitting to benchmark, answer: {ans}")

    exit_stack = AsyncExitStack()
    logger.info("Using HTTP, connecting to server.")
    server_url = langgraph_tool_config.submit_mcp_url
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool(
        "submit",
        arguments={
            "ans": ans,
        },
    )
    await exit_stack.aclose()
    return Command(
        update={
            "submitted": True,
            "messages": [ToolMessage(f"Submission complete. No further action is needed.", tool_call_id=tool_call_id)],
        }
    )


@tool(description=submit_tool_docstring)
async def fake_submit_tool(ans: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    # makes http call to benchmark submission server
    logging.info(f"_NOT_ submitting to benchmark, answer: {ans}")
    logger.info(f"This method is to only change the state[submitted] value.")
    logger.info(f"mitigation submission is done out side of agent logic, for retry")

    return Command(
        update={
            "submitted": True,
            "messages": [ToolMessage(f"Submission complete. No further action is needed.", tool_call_id=tool_call_id)],
        }
    )


async def manual_submit_tool(ans: str) -> str:
    # makes http call to benchmark submission server
    logging.info(f"_manually_ submitting to benchmark, answer: {ans}")

    exit_stack = AsyncExitStack()
    logger.info("Using HTTP, connecting to server.")
    server_url = langgraph_tool_config.submit_mcp_url
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool(
        "submit",
        arguments={
            "ans": ans,
        },
    )
    await exit_stack.aclose()
    logger.info("Submission complete. No further action is needed.")
    return "Submitted"
