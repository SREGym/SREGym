import logging
from typing import Annotated
import time
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool, InjectedToolCallId

wait_tool_docstring = """
Use this tool to wait for you action to take effect. The upper limit is 120 seconds. 
    Any value above 120 seconds will be truncated to 120 seconds. If you call this tool
    along with other tools in your tool_calls list, this tool will be scheduled to the 
    last for execution.

    Args:
        seconds (int): Number of seconds to wait.
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@tool(description=wait_tool_docstring)
def wait_tool(seconds: int,
              tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:

    message = ""
    if seconds > 120:
        message += f"Request waiting {seconds} sec, but the maximum wait time is 120 sec. " \
                   f"Will be truncated to 120 sec."
    seconds = max(0, min(seconds, 120))
    time.sleep(seconds)
    message += f"wait_tool has been called to wait {seconds} seconds."
    logger.info(message)
    return Command(update={
        "messages": [
            ToolMessage(message,
                        tool_call_id=tool_call_id)
        ]
    })
