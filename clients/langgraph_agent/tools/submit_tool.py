from typing import Annotated
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool, InjectedToolCallId

submit_tool_docstring = """
Use this tool to submit your answer to the assigned tasks. You can give partial answer or empty answer
    (still of type dict) if you can not solve all of them.

    Args:
        ans (dict): Your answers to submit. For each item in it, the key corresponds to the task name and the value
            corresponds to your answer to this task.
"""


@tool(description=submit_tool_docstring)
def submit_tool(ans: dict,
                tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:

    return Command(update={
        "submitted": True,
        "ans": ans,
        "messages": [
            ToolMessage(f"Submission complete. No further action is needed.",
                        tool_call_id=tool_call_id)
        ]
    })
