import ast
import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from mcp import ClientSession
from mcp.client.sse import sse_client

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

    max_retries = 3
    timeout_seconds = 300  # 5 minutes per attempt
    result = None
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Submission attempt {attempt}/{max_retries} (timeout: {timeout_seconds}s)")

    exit_stack = AsyncExitStack()
            try:
                async def _submit_with_timeout():
                    server_url = langgraph_tool_config.submit_mcp_url
    logger.info("Using HTTP, connecting to server.")
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

                    call_result = await session.call_tool(
        "submit",
        arguments={
            "ans": ans,
        },
    )
                    return call_result
                
                # Execute with timeout
                call_result = await asyncio.wait_for(
                    _submit_with_timeout(),
                    timeout=timeout_seconds
                )
                
                result_text = call_result.content[0].text
                result = ast.literal_eval(result_text)
                await exit_stack.aclose()
                break  # Success, exit retry loop
                
            except asyncio.TimeoutError:
                await exit_stack.aclose()
                error_msg = f"Submission timeout after {timeout_seconds}s on attempt {attempt}/{max_retries}"
                logger.warning(error_msg)
                last_error = TimeoutError(error_msg)
                
                if attempt < max_retries:
                    wait_time = min(5 * attempt, 30)
                    logger.info(f"Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Final attempt failed, return error to agent
                    return Command(
                        update={
                            "num_steps": state["num_steps"] - 1,
                            "messages": [
                                ToolMessage(
                                    content=f"Submission timeout after {max_retries} attempts. Please try again.",
                                    tool_call_id=tool_call_id
                                ),
                            ],
                        }
                    )
                    
            except Exception as e:
    await exit_stack.aclose()
                error_str = str(e).lower()
                last_error = e
                
                # Check if it's a timeout-related error
                if "timeout" in error_str or "deadline" in error_str or "cancelled" in error_str:
                    if attempt < max_retries:
                        wait_time = min(5 * attempt, 30)
                        logger.info(f"Timeout error detected. Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        return Command(
                            update={
                                "num_steps": state["num_steps"] - 1,
                                "messages": [
                                    ToolMessage(
                                        content=f"Submission timeout after {max_retries} attempts: {str(e)}",
                                        tool_call_id=tool_call_id
                                    ),
                                ],
                            }
                        )
                else:
                    # Non-timeout error, try to extract result or re-raise
                    raise
                    
        except Exception as e:
            last_error = e
            if attempt == max_retries:
                # Final attempt, return error
                logger.error(f"Submission failed after {max_retries} attempts: {e}")
                return Command(
                    update={
                        "num_steps": state["num_steps"] - 1,
                        "messages": [
                            ToolMessage(
                                content=f"Submission failed after {max_retries} attempts: {str(e)}",
                                tool_call_id=tool_call_id
                            ),
                        ],
                    }
                )
            # Continue to next retry
            wait_time = min(5 * attempt, 30)
            logger.info(f"Error on attempt {attempt}, retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
            continue
    
    # If we get here without result, something went wrong
    if result is None:
        error_msg = f"Submission failed: {str(last_error) if last_error else 'unknown error'}"
        logger.error(error_msg)
        return Command(
            update={
                "num_steps": state["num_steps"] - 1,
                "messages": [
                    ToolMessage(
                        content=error_msg,
                        tool_call_id=tool_call_id
                    ),
                ],
            }
        )

    # Process the result
    if result["status"] != "200":
        # Check if the error is because the stage is already "done"
        error_detail = result.get("detail", "") or str(result)
        if "done" in error_detail.lower() or "Cannot submit at stage" in error_detail:
            logger.info(f"Submission stage is already 'done'. Problem is complete. Setting submitted=True to stop agent.")
            return Command(
                update={
                    "submitted": True,
                    "messages": [
                        ToolMessage(
                            content=f"Problem is already complete (stage: done). No further submission needed.", 
                            tool_call_id=tool_call_id
                        ),
                    ],
                }
            )
        
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
            "messages": [ToolMessage(f"Submission complete. No further action is needed.", tool_call_id=tool_call_id)],
        }
    )


@tool("f_submit_tool", description=submit_tool_docstring)
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


async def manual_submit_tool(ans: str, max_retries: int = 3, timeout_seconds: int = 300) -> str:
    """
    Submit answer to benchmark server with retry logic and timeout handling.
    
    Args:
        ans: Answer to submit
        max_retries: Maximum number of retry attempts (default: 3)
        timeout_seconds: Timeout for each attempt in seconds (default: 300 = 5 minutes)
    
    Returns:
        "Submitted" on success
    
    Raises:
        Exception: If all retries fail
    """
    logging.info(f"_manually_ submitting to benchmark, answer: {ans}")

    server_url = langgraph_tool_config.submit_mcp_url
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Submission attempt {attempt}/{max_retries} (timeout: {timeout_seconds}s)")
            
            exit_stack = AsyncExitStack()
            try:
                # Use asyncio.wait_for to add timeout protection
                async def _submit_with_timeout():
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool(
        "submit",
        arguments={
            "ans": ans,
        },
    )
                    return result
                
                # Execute with timeout
                result = await asyncio.wait_for(
                    _submit_with_timeout(),
                    timeout=timeout_seconds
                )
                
    await exit_stack.aclose()
                logger.info(f"✅ Submission successful on attempt {attempt}")
    logger.info("Submission complete. No further action is needed.")
    return "Submitted"
                
            except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                await exit_stack.aclose()
                error_type = "Timeout" if isinstance(e, asyncio.TimeoutError) else "Cancelled"
                error_msg = f"Submission {error_type.lower()} after {timeout_seconds}s on attempt {attempt}/{max_retries}"
                logger.warning(error_msg)
                last_error = e
                
                if attempt < max_retries:
                    wait_time = min(5 * attempt, 30)  # Exponential backoff, max 30s
                    logger.info(f"Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    raise TimeoutError(f"Submission failed after {max_retries} attempts: {error_msg}")
            
            except ExceptionGroup as e:
                # Handle TaskGroup exceptions (which wrap CancelledError from timeouts)
                await exit_stack.aclose()
                # Check if it's a timeout-related cancellation
                if any("deadline exceeded" in str(exc).lower() or "cancelled" in str(exc).lower() 
                       for exc in (e.exceptions if hasattr(e, 'exceptions') else [e])):
                    error_msg = f"Submission timeout (TaskGroup exception) after {timeout_seconds}s on attempt {attempt}/{max_retries}"
                    logger.warning(error_msg)
                    last_error = TimeoutError(error_msg)
                    
                    if attempt < max_retries:
                        wait_time = min(5 * attempt, 30)  # Exponential backoff, max 30s
                        logger.info(f"Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        raise TimeoutError(f"Submission failed after {max_retries} attempts: {error_msg}")
                else:
                    # Not a timeout, re-raise
                    raise
                    
            except Exception as e:
                await exit_stack.aclose()
                error_msg = f"Submission error on attempt {attempt}/{max_retries}: {str(e)}"
                logger.warning(error_msg)
                last_error = e
                
                # Check if it's a timeout-related error
                if "timeout" in str(e).lower() or "deadline" in str(e).lower() or "cancelled" in str(e).lower():
                    if attempt < max_retries:
                        wait_time = min(5 * attempt, 30)
                        logger.info(f"Timeout error detected. Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise TimeoutError(f"Submission failed after {max_retries} attempts due to timeout: {error_msg}")
                else:
                    # Non-timeout error, re-raise immediately
                    raise
                    
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"❌ Submission failed after {max_retries} attempts")
                raise
            # Continue to next retry
            continue
    
    # Should not reach here, but just in case
    if last_error:
        raise last_error
    raise Exception("Submission failed: unknown error")
