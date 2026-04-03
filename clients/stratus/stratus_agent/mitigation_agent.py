import logging
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import StateSnapshot

from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_agent.state import State
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from llm_backend.init_backend import get_llm_backend_for_agent

logger = logging.getLogger("all.stratus.mitigation")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class MitigationAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = logging.getLogger("all.stratus.mitigation")

    async def force_submit(self, state: State):
        self.logger.warning(f"Agent reached step limit ({self.max_step}), forcing submission via f_submit.")
        self.logger.info("Force submit: calling f_submit (no real benchmark submission; real submission deferred to driver).")
        return {"submitted": True, "messages": [HumanMessage("Step limit reached. Submitting via f_submit.")]}


def build_default_mitigation_agent():
    file_parent_dir = Path(__file__).resolve().parent
    mitigation_agent_config_path = file_parent_dir.parent / "configs" / "mitigation_agent_config.yaml"
    mitigation_agent_config = yaml.safe_load(mitigation_agent_config_path.read_text())
    mitigation_agent_max_step = mitigation_agent_config["max_step"]
    mitigation_agent_prompt_path = file_parent_dir.parent / "configs" / mitigation_agent_config["prompts_path"]

    mitigation_agent_sync_tools = []
    mitigation_agent_async_tools = []
    mitigation_submit_tool = None
    if mitigation_agent_config["sync_tools"] is not None:
        for sync_tool_struct in mitigation_agent_config["sync_tools"]:
            mitigation_agent_sync_tools.append(str_to_tool(sync_tool_struct))
    else:
        mitigation_agent_sync_tools = None
    if mitigation_agent_config["async_tools"] is not None:
        for async_tool_struct in mitigation_agent_config["async_tools"]:
            tool = str_to_tool(async_tool_struct)
            mitigation_agent_async_tools.append(tool)
            if async_tool_struct["name"] in {"submit_tool", "f_submit_tool"}:
                mitigation_submit_tool = tool
    else:
        mitigation_agent_async_tools = None

    if mitigation_submit_tool is None:
        raise ValueError("Mitigation agent config must include either submit_tool or f_submit_tool.")

    mitigation_agent = MitigationAgent(
        llm=get_llm_backend_for_agent(),
        max_step=mitigation_agent_max_step,
        sync_tools=mitigation_agent_sync_tools,
        async_tools=mitigation_agent_async_tools,
        submit_tool=mitigation_submit_tool,
    )
    mitigation_agent.build_agent()
    return mitigation_agent, mitigation_agent_prompt_path, mitigation_agent_max_step


def generate_run_summary(last_state: StateSnapshot, summary_system_prompt) -> str:
    """
    Returns a summary and reflection of the given last run state.

    Args:
        last_state (StateSnapshot): the state from last run
    Returns:
        a string representing the LLM's summary and reflection
    """
    llm = get_llm_backend_for_agent()
    logger.info("asking LLM to summarize and reflect last run")
    last_run_msgs = last_state.values.get("messages", None)
    if last_run_msgs is None:
        raise RuntimeError("StateSnapshot must contain messages!")
    summary_input_messages = [
        SystemMessage(summary_system_prompt),
        HumanMessage(f"Here are the list of messages happened in the last conversation. \n\n {last_run_msgs}"),
    ]
    res = llm.inference(summary_input_messages)
    return res.content


async def single_run_with_predefined_prompts(init_prompts):
    agent, prompt_path, max_step = build_default_mitigation_agent()
    last_state, graph_events = await agent.arun(init_prompts)
    logger.info("Clearing agent's memory")
    agent.clear_memory()
    return agent, last_state, graph_events


async def retry_run_with_feedback(feedback_prompts):
    agent, prompt_path, max_step = build_default_mitigation_agent()
    last_state, graph_events = await agent.arun(feedback_prompts)
    logger.info("Clearing agent's memory")
    agent.clear_memory()
    return agent, last_state, graph_events


if __name__ == "__main__":
    logger.info("Mitigation agent does not support running as a module.")
