import logging
from dataclasses import dataclass

from clients.stratus.stratus_utils.str_to_tool import get_client

logger = logging.getLogger("all.stratus.rollback")
logger.propagate = True
logger.setLevel(logging.DEBUG)


@dataclass
class RollbackResult:
    steps: int
    rollback_stack: str


async def perform_rollback(commands: list[str], session_id: str | None = None) -> RollbackResult:
    """Deterministically roll back state-changing kubectl commands via the MCP stack."""
    n = len(commands)
    if n == 0:
        logger.info("[ROLLBACK] No commands to roll back.")
        return RollbackResult(steps=0, rollback_stack="nothing to roll back")

    logger.info(f"[ROLLBACK] Using MCP session_id: {session_id}")
    logger.info(f"[ROLLBACK] Rolling back {n} command(s): {commands}")
    results = []
    for i, cmd in enumerate(reversed(commands)):
        logger.info(f"[ROLLBACK] Step {i + 1}/{n}: reversing '{cmd}'")
        client = get_client(session_id)
        try:
            async with client:
                result = await client.call_tool("rollback_command")
            result_text = "\n".join(part.text for part in result)
        except Exception as e:
            result_text = f"Error during rollback step {i + 1}: {e}"
            logger.error(f"[ROLLBACK] Step {i + 1}/{n} error: {e}")
        logger.info(f"[ROLLBACK] Step {i + 1}/{n} result: {result_text}")
        results.append(result_text)

    logger.info(f"[ROLLBACK] Done. Rolled back {n} command(s).")
    return RollbackResult(steps=n, rollback_stack="\n".join(results))
