import logging

from mcp_server.kubectl_server_helper.rollback_tool import RollbackNode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        # logging.FileHandler("action_stack.log"),
        logging.StreamHandler()  # This will output to console too
    ],
)


class ActionStack:
    """Rollback history for one MCP session, newest last.

    Tool handlers now run their blocking work in a worker thread
    (mcp_server/kubectl_mcp_tools.py), so two calls on the same session can
    reach this object at the same time, where before the event loop
    serialised them.

    push is safe on its own: list.append is one operation and does not
    interleave. pop was not. It tested the stack and then popped in a
    separate step, so two threads draining the last entry could both pass
    the test and the second would pop an empty list and raise IndexError.
    It now pops once and decides afterwards, which is the same single
    operation push relies on.

    What concurrency still changes is order. Two mutating commands issued
    together push in completion order, not issue order, so a later rollback
    would undo them in the wrong sequence. That is not a lock away: it needs
    the caller to say which order it meant. No caller does that today, since
    a diagnosis run issues read-only commands, which never reach push, and
    mitigation runs as one sequential agent. Revisit this if that stops
    being true.
    """

    def __init__(self):
        self.stack = []

    def push(self, node: RollbackNode):
        """Push a new action onto the stack."""
        self.stack.append(node)
        logging.info(f"Pushed action onto stack: {node}")

    def pop(self):
        """Pop the last action from the stack (for rollback).

        One pop, then log what it got. Testing the stack first and popping
        afterwards let two threads both pass the test and the second pop an
        empty list.
        """
        try:
            node = self.stack.pop()
        except IndexError:
            return None

        logging.info(f"Popped action from stack: {node}")

        return node

    def peek(self):
        """View the last action without removing it."""
        return self.stack[-1] if self.stack else None

    def clear(self):
        """Clear the entire stack."""
        self.stack = []

    def __str__(self) -> str:
        if not self.stack:
            return "ActionStack: [Empty]"

        result = ["ActionStack:"]
        for i, node in enumerate(reversed(self.stack)):
            index = len(self.stack) - i - 1
            result.append(f"  [{index}] {node}")

        return "\n".join(result)

    def __repr__(self) -> str:
        return self.__str__()
