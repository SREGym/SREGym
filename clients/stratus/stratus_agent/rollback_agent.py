import asyncio
import logging
from pathlib import Path

import yaml

from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_utils.get_starting_prompt import get_starting_prompts
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from llm_backend.init_backend import get_llm_backend_for_agent

logger = logging.getLogger("all.stratus.rollback")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class RollbackAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = logging.getLogger("all.stratus.rollback")


async def main():
    file_parent_dir = Path(__file__).resolve().parent
    rollback_agent_config_path = file_parent_dir.parent / "configs" / "rollback_agent_config.yaml"
    rollback_agent_config = yaml.safe_load(open(rollback_agent_config_path))
    max_step = rollback_agent_config["max_step"]
    prompt_path = file_parent_dir.parent / "configs" / rollback_agent_config["prompts_path"]

    sync_tools = []
    async_tools = []
    if rollback_agent_config["sync_tools"] is not None:
        for sync_tool_struct in rollback_agent_config["sync_tools"]:
            sync_tools.append(str_to_tool(sync_tool_struct))
    else:
        sync_tools = None
    if rollback_agent_config["async_tools"] is not None:
        for async_tool_struct in rollback_agent_config["async_tools"]:
            async_tools.append(str_to_tool(async_tool_struct))
    else:
        async_tools = None

    submit_tool = str_to_tool(
        {
            "name": "submit_tool",
            "description": """
                The tool to submit benchmark results

                    Args:
                        ans (str): the answer you would like to submit to the benchmark
        """,
        }
    )

    agent = RollbackAgent(
        llm=get_llm_backend_for_agent(),
        max_step=max_step,
        sync_tools=sync_tools,
        async_tools=async_tools,
        submit_tool=submit_tool,
    )
    agent.build_agent()

    last_state, graph_events = await agent.arun(get_starting_prompts(prompt_path, max_step=max_step))
    agent.clear_memory()
    return agent, last_state, graph_events


if __name__ == "__main__":
    asyncio.run(main())
