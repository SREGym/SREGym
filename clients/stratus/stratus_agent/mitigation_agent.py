import logging
from pathlib import Path

import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
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
        self.logger.warning(f"Agent reached step limit ({self.max_step}), forcing submission.")
        prompt = HumanMessage("You have reached your step limit. Please submit your best answer using the submit tool.")
        ai_message = self.llm.inference(messages=state["messages"] + [prompt], tools=[self.submit_tool])

        if isinstance(ai_message, AIMessage) and ai_message.tool_calls:
            tool_call = ai_message.tool_calls[0]
            if tool_call.get("name") == self.submit_tool.name:
                ans = tool_call.get("args", {}).get("ans", "")
            else:
                self.logger.warning(f"LLM called unexpected tool '{tool_call.get('name')}' during force submit.")
                ans = None
        else:
            ans = None

        if ans is None:
            self.logger.warning("LLM did not call the submit tool during force submit. Extracting plain-text answer.")
            plain_prompt = HumanMessage("Please write out your best answer as plain text.")
            # Strip tool_calls from ai_message before using it in the next inference.
            # The Bedrock/Claude API requires every tool_use block to be immediately
            # followed by a tool_result block; including an ai_message with tool_calls
            # here (without results) causes a BadRequestError.
            if isinstance(ai_message, AIMessage) and ai_message.tool_calls:
                ai_message_no_tools = AIMessage(content=ai_message.content)
            else:
                ai_message_no_tools = ai_message
            plain_response = self.llm.inference(
                messages=state["messages"] + [prompt, ai_message_no_tools, plain_prompt]
            )
            ans = plain_response.content if isinstance(plain_response, AIMessage) else ""

        self.logger.info(
            f"Force submit: signaling transaction attempt with answer: {ans!r}. Real submission deferred to driver."
        )
        return {"submitted": True, "messages": [prompt]}


def build_default_mitigation_agent():
    file_parent_dir = Path(__file__).resolve().parent
    mitigation_agent_config_path = file_parent_dir.parent / "configs" / "mitigation_agent_config.yaml"
    mitigation_agent_config = yaml.safe_load(mitigation_agent_config_path.read_text())
    mitigation_agent_max_step = mitigation_agent_config["max_step"]
    mitigation_agent_prompt_path = file_parent_dir.parent / "configs" / mitigation_agent_config["prompts_path"]

    mitigation_agent_sync_tools = []
    mitigation_agent_async_tools = []
    if mitigation_agent_config["sync_tools"] is not None:
        for sync_tool_struct in mitigation_agent_config["sync_tools"]:
            mitigation_agent_sync_tools.append(str_to_tool(sync_tool_struct))
    else:
        mitigation_agent_sync_tools = None
    if mitigation_agent_config["async_tools"] is not None:
        for async_tool_struct in mitigation_agent_config["async_tools"]:
            mitigation_agent_async_tools.append(str_to_tool(async_tool_struct))
    else:
        mitigation_agent_async_tools = None

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

    mitigation_agent = MitigationAgent(
        llm=get_llm_backend_for_agent(),
        max_step=mitigation_agent_max_step,
        sync_tools=mitigation_agent_sync_tools,
        async_tools=mitigation_agent_async_tools,
        submit_tool=submit_tool,
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
