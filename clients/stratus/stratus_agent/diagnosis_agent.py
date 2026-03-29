import logging
from pathlib import Path

import yaml

from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from llm_backend.init_backend import get_llm_backend_for_agent

logger = logging.getLogger("all.stratus.diagnosis")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class DiagnosisAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = logging.getLogger("all.stratus.diagnosis")


def build_default_diagnosis_agent():
    file_parent_dir = Path(__file__).resolve().parent
    diagnosis_agent_config_path = file_parent_dir.parent / "configs" / "diagnosis_agent_config.yaml"
    diagnosis_agent_config = yaml.safe_load(diagnosis_agent_config_path.read_text())
    max_step = diagnosis_agent_config["max_step"]
    prompt_path = file_parent_dir.parent / "configs" / diagnosis_agent_config["prompts_path"]

    sync_tools = []
    async_tools = []
    if diagnosis_agent_config["sync_tools"] is not None:
        for sync_tool_struct in diagnosis_agent_config["sync_tools"]:
            sync_tools.append(str_to_tool(sync_tool_struct))
    else:
        sync_tools = None
    if diagnosis_agent_config["async_tools"] is not None:
        for async_tool_struct in diagnosis_agent_config["async_tools"]:
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

    agent = DiagnosisAgent(
        llm=get_llm_backend_for_agent(),
        max_step=max_step,
        sync_tools=sync_tools,
        async_tools=async_tools,
        submit_tool=submit_tool,
    )
    agent.build_agent()
    return agent, prompt_path, max_step


async def single_run_with_predefined_prompts(init_prompts):
    agent, prompt_path, max_step = build_default_diagnosis_agent()
    last_state, graph_events = await agent.arun(init_prompts)
    logger.info("Clearing agent's memory")
    agent.clear_memory()
    return agent, last_state, graph_events
