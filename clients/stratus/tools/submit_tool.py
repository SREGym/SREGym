import ast
import logging
from contextlib import AsyncExitStack
from typing import Annotated

import httpx
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from clients.stratus.stratus_agent.state import State

submit_tool_docstring = """
Use this tool to submit your answer to the assigned tasks. You can give partial answer or empty answer
    (still of type dict) if you can not solve all of them.

    Args:
        ans (string): the answer you would like to submit
"""

rollback_submit_tool_docstring = """
The tool to submit after you rolled back all the changes.
"""
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

langgraph_tool_config = LanggraphToolConfig()


@tool(description=submit_tool_docstring)
async def submit_tool(
    ans: str, state: Annotated[State, InjectedState], tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    # makes http call to benchmark submission server
    logging.info(f"submitting to benchmark, answer: {ans}")

    exit_stack = AsyncExitStack()
    server_url = langgraph_tool_config.submit_mcp_url
    try:
        logger.info("Using HTTP, connecting to server.")
        http_transport = await exit_stack.enter_async_context(
            sse_client(
                url=server_url,
                timeout=30,
                sse_read_timeout=600,
            )
        )
        session = await exit_stack.enter_async_context(ClientSession(*http_transport))

        await session.initialize()

        result = await session.call_tool(
            "submit",
            arguments={
                "ans": ans,
            },
        )
        result = result.content[0].text
        result = ast.literal_eval(result)

        if result["status"] != "200":
            logger.info(f"HTTP submission failed: {result}")
            logger.info("we don't set submitted to True, to force agent retry submission. \n")
            logger.info("giving agent another change by decrementing step count")
            return Command(
                update={
                    "num_steps": state["num_steps"] - 1,
                    "messages": [
                        ToolMessage(content=f"HTTP submission failed: {result}", tool_call_id=tool_call_id),
                    ],
                }
            )
        logger.info("submission succeeded.")
        return Command(
            update={
                "submitted": True,
                "messages": [
                    ToolMessage(f"Submission complete. No further action is needed.", tool_call_id=tool_call_id)
                ],
            }
        )
    except Exception as e:
        logger.error(f"Submission failed: {e}")
        return Command(
            update={
                "num_steps": state["num_steps"] - 1,
                "messages": [
                    ToolMessage(content=f"Submission failed: {e}", tool_call_id=tool_call_id),
                ],
            }
        )
    finally:
        await exit_stack.aclose()


@tool("f_submit_tool", description=submit_tool_docstring)
async def fake_submit_tool(ans: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    # makes http call to benchmark submission server
    logging.info(f"_NOT_ submitting to benchmark, answer: {ans}")
    logging.info(f"This method is to only change the state[submitted] value.")
    logging.info(f"mitigation submission is done out side of agent logic, for retry")

    return Command(
        update={
            "submitted": True,
            "messages": [ToolMessage(f"Submission complete. No further action is needed.", tool_call_id=tool_call_id)],
        }
    )


@tool("r_submit_tool", description=rollback_submit_tool_docstring)
async def rollback_submit_tool(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    logger.info("rollback agent submits")
    logger.info(f"This method is to only change the state[submitted] value.")

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
