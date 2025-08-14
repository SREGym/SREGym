import asyncio
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END, START

from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools
from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_agent.diagnosis_agent import main as diagnosis_agent_main
from clients.stratus.stratus_agent.localization_agent import main as localization_agent_main
from clients.stratus.stratus_utils.get_logger import get_logger
from clients.stratus.stratus_utils.get_starting_prompt import get_starting_prompts
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from clients.stratus.tools.stratus_tool_node import StratusToolNode

logger = get_logger()


async def main():
    # run diagnosis agent 2 times
    # here, running the file's main function should suffice.
    # 1 for noop diagnosis
    logger.info("*" * 25 + "Starting [diagnosis agent] for [NOOP detection]" + "*" * 25)
    await diagnosis_agent_main()
    logger.info("*" * 25 + "Finished [diagnosis agent]" + "*" * 25)

    # 1 for faulty diagnosis
    logger.info("*" * 25 + "Starting [diagnosis agent] for [Faulty detection]" + "*" * 25)
    await diagnosis_agent_main()
    logger.info("*" * 25 + "Finished [diagnosis agent]" + "*" * 25)

    # run localization agent 1 time for localization
    # (BTS it's just diagnosis agent with different prompts)
    # here, running the file's main function should suffice
    logger.info("*" * 25 + "Starting [localization agent] for [localization]" + "*" * 25)
    await localization_agent_main()

    # run rollback, reflect, and retry for mitigation and rollback agent
    pass


if __name__ == "__main__":
    asyncio.run(main())
