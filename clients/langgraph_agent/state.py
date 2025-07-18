from langgraph.graph import add_messages
from typing_extensions import Annotated, TypedDict


class State(TypedDict):
    # Messages have the type "list". The `add_messages` function
    # in the annotation defines how this state key should be updated
    # (in this case, it appends messages to the list, rather than overwriting them)
    messages: Annotated[list, add_messages]
    workdir: str
    curr_file: str
    curr_line: int
    num_rounds: int
    submitted: bool
    ans: dict
