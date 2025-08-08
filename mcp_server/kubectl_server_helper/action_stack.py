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
    def __init__(self):
        self.stack = []

    def push(self, node: RollbackNode):
        """Push a new action onto the stack."""
        self.stack.append(node)
        logging.info(f"Pushed action onto stack: {node}")

    def pop(self):
        """Pop the last action from the stack (for rollback)."""
        if self.stack:
            logging.info(f"Popped action from stack: {self.stack[-1]}")

        return self.stack.pop() if self.stack else None

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
