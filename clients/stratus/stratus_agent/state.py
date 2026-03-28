import operator
from typing import Annotated

from langgraph.graph import add_messages
from typing_extensions import TypedDict


class State(TypedDict):
    # Messages have the type "list". The `add_messages` function
    # in the annotation defines how this state key should be updated
    # (in this case, it appends messages to the list, rather than overwriting them)
    messages: Annotated[list, add_messages]
    # workdir: str
    # curr_file: str
    # curr_line: int
    # number or rounds used to finish assigned tasks
    # num_rounds: int
    num_steps: int
    # number of rounds used for rectifying submission
    # rec_submission_rounds: int
    submitted: bool
    # submit_tried: bool
    # ans: dict
    rollback_stack: str
    # stack of state-changing kubectl commands executed by the mitigation agent
    executed_commands: Annotated[list[str], operator.add]
