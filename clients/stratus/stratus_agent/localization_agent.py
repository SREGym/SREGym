import asyncio
import logging
from pathlib import Path

import yaml

from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools
from clients.stratus.stratus_agent.diagnosis_agent import DiagnosisAgent
from clients.stratus.stratus_utils.get_starting_prompt import get_starting_prompts
from clients.stratus.stratus_utils.str_to_tool import str_to_tool

logger = logging.getLogger("all.stratus.localization")
logger.propagate = True
logger.setLevel(logging.DEBUG)


def build_default_localization_agent():
    file_parent_dir = Path(__file__).resolve().parent
    localization_agent_config_path = file_parent_dir.parent / "configs" / "localization_agent_config.yaml"
    localization_agent_config = yaml.safe_load(open(localization_agent_config_path, "r"))
    max_step = localization_agent_config["max_step"]
    prompt_path = file_parent_dir.parent / "configs" / localization_agent_config["prompts_path"]
    sync_tools = []
    async_tools = []
    tool_descriptions = ""
    if localization_agent_config["sync_tools"] is not None:
        for sync_tool_struct in localization_agent_config["sync_tools"]:
            sync_tools.append(str_to_tool(sync_tool_struct))
            tool_descriptions += (
                f"tool name: {sync_tool_struct["name"]}"
                + "\n\n"
                + f"tool descriptions {sync_tool_struct["description"]}"
                + "\n\n"
            )
    else:
        sync_tools = None
    if localization_agent_config["async_tools"] is not None:
        for async_tool_struct in localization_agent_config["async_tools"]:
            async_tools.append(str_to_tool(async_tool_struct))
            tool_descriptions += (
                f"tool name: {async_tool_struct["name"]}"
                + "\n\n"
                + f"tool description: {async_tool_struct["description"]}"
                + "\n\n"
            )
    else:
        async_tools = None

    submit_tool = str_to_tool(
        {
            "name": "submit_tool",
            "description": """
                The tool to submit benchmark results

                    Args:
                        ans (str): Use natural language to describe the root cause of the failure.
        """,
        }
    )

    agent = DiagnosisAgent(
        llm=get_llm_backend_for_tools(),
        max_step=max_step,
        sync_tools=sync_tools,
        async_tools=async_tools,
        submit_tool=submit_tool,
        tool_descs=tool_descriptions,
    )
    agent.build_agent()
    agent.save_agent_graph_to_png()
    return agent, prompt_path, max_step


async def single_run_with_predefined_prompts(init_prompts):
    agent, prompt_path, max_step = build_default_localization_agent()
    res = await agent.arun(init_prompts)
    logger.info("Clearing agent's memory")
    agent.clear_memory()
    return agent, res
