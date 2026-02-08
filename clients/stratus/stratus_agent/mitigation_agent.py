import logging
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END, START
from langgraph.types import StateSnapshot

from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from clients.stratus.tools.stratus_tool_node import StratusToolNode
from llm_backend.init_backend import get_llm_backend_for_tools

logger = logging.getLogger("all.stratus.mitigation")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class MitigationAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_node = None
        self.max_step = kwargs.get("max_step", 20)
        self.loop_count = 0
        self.logger = logging.getLogger("all.stratus.mitigation")

    def build_agent(self):
        self.tool_node = StratusToolNode(async_tools=self.async_tools, sync_tools=self.sync_tools)

        self.graph_builder.add_node(self.thinking_prompt_inject_node, self.llm_thinking_prompt_inject_step)
        self.graph_builder.add_node(self.tool_calling_prompt_inject_node, self.llm_tool_call_prompt_inject_step)
        self.graph_builder.add_node(self.thinking_node, self.llm_thinking_step)
        self.graph_builder.add_node(self.tool_calling_node, self.llm_tool_call_step)
        self.graph_builder.add_node(self.process_tool_call_node, self.tool_node)
        self.graph_builder.add_node(self.post_round_process_node, self.post_round_process)
        self.graph_builder.add_node(self.force_submit_prompt_inject_node, self.llm_force_submit_thinking_step)
        self.graph_builder.add_node(self.force_submit_tool_call_node, self.llm_force_submit_tool_call_step)
        self.graph_builder.add_node(self.force_submit_tool_execute_node, self.llm_force_submit_tool_execute_node)

        self.graph_builder.add_edge(START, self.thinking_prompt_inject_node)
        self.graph_builder.add_edge(self.thinking_prompt_inject_node, self.thinking_node)
        self.graph_builder.add_edge(self.thinking_node, self.tool_calling_prompt_inject_node)
        self.graph_builder.add_edge(self.tool_calling_prompt_inject_node, self.tool_calling_node)
        self.graph_builder.add_edge(self.tool_calling_node, self.process_tool_call_node)
        self.graph_builder.add_edge(self.process_tool_call_node, self.post_round_process_node)
        self.graph_builder.add_conditional_edges(
            self.process_tool_call_node,
            self.should_submit_router,
            {
                self.force_submit_prompt_inject_node: self.force_submit_prompt_inject_node,
                self.post_round_process_node: self.post_round_process_node,
            },
        )
        # TODO: Before submitting, run oracle to see if really mitigated.
        self.graph_builder.add_edge(self.force_submit_prompt_inject_node, self.force_submit_tool_call_node)
        self.graph_builder.add_edge(self.force_submit_tool_call_node, self.force_submit_tool_execute_node)
        self.graph_builder.add_edge(self.force_submit_tool_execute_node, END)
        self.graph_builder.add_edge(self.post_round_process_node, END)

        self.memory_saver = MemorySaver()
        self.graph = self.graph_builder.compile(checkpointer=self.memory_saver)

    async def arun(self, starting_prompts):
        """
        Async running an agent

        Args:
            starting_prompts (dict): The data inside the dict will be filled into the prompts.

        Returns:
            final state of the agent running, including messages and other state values.
        """
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")

        if len(starting_prompts) == 0:
            raise ValueError("No prompts used to start the conversation!")

        # Log starting prompts
        all_init_prompts = ""
        for prompt in starting_prompts:
            all_init_prompts += prompt.content + "\n"

        graph_events = []
        while True:
            graph_config = {"configurable": {"thread_id": "1"}}
            logger.info(f"{'-' * 20} [Loop {self.loop_count}] {'-' * 20}")
            last_state = self.graph.get_state(config=graph_config)
            if len(last_state.values) != 0:
                logger.debug(f"[Loop {self.loop_count}] There were last {len(last_state.values)} states.")
                # this is all the previous msgs the agent had, we just inherit them in the next graph traversal
                state = last_state.values
            else:
                logger.debug(f"[Loop {self.loop_count}] There were no states.")
                # fresh agent start, init state here
                state = {
                    "messages": starting_prompts,
                    # "workdir": "",
                    # "curr_file": "",
                    # "curr_line": 0,
                    "num_steps": 0,
                    # "rec_submission_rounds": 0,
                    # "submit_tried": False,
                    "submitted": False,
                    # "ans": dict(),
                    "rollback_stack": "",
                }

            async for event in self.graph.astream(
                state,
                # recursion_limit could be as large as possible as we have our own limit.
                config={"recursion_limit": 10000, "configurable": {"thread_id": "1"}, "callbacks": [self.callback]},
                stream_mode="values",
            ):
                if (not graph_events) or event["messages"] != graph_events[-1]["messages"]:
                    event["messages"][-1].pretty_print()
                graph_events.append(event)
            last_state = self.graph.get_state(config=graph_config)
            if last_state.values["submitted"]:
                logger.info(f"[Loop {self.loop_count}] Agent submitted, breaking loop.")
                # Ensure the final state is included in graph_events
                if not graph_events or last_state.values["messages"] != graph_events[-1]["messages"]:
                    graph_events.append(last_state.values)
                break

            self.loop_count += 1

        return last_state, graph_events


def build_default_mitigation_agent():
    # agent config and init setup
    file_parent_dir = Path(__file__).resolve().parent
    mitigation_agent_config_path = file_parent_dir.parent / "configs" / "mitigation_agent_config.yaml"
    mitigation_agent_config = yaml.safe_load(open(mitigation_agent_config_path, "r"))
    mitigation_agent_max_step = mitigation_agent_config["max_step"]
    mitigation_agent_prompt_path = file_parent_dir.parent / "configs" / mitigation_agent_config["prompts_path"]

    mitigation_agent_sync_tools = []
    mitigation_agent_async_tools = []
    mitigation_agent_tool_descriptions = ""
    if mitigation_agent_config["sync_tools"] is not None:
        for sync_tool_struct in mitigation_agent_config["sync_tools"]:
            mitigation_agent_sync_tools.append(str_to_tool(sync_tool_struct))
            mitigation_agent_tool_descriptions += (
                f"tool name: {sync_tool_struct["name"]}"
                + "\n\n"
                + f"tool descriptions {sync_tool_struct["description"]}"
                + "\n\n"
            )
    else:
        mitigation_agent_sync_tools = None
    if mitigation_agent_config["async_tools"] is not None:
        for async_tool_struct in mitigation_agent_config["async_tools"]:
            mitigation_agent_async_tools.append(str_to_tool(async_tool_struct))
            mitigation_agent_tool_descriptions += (
                f"tool name: {async_tool_struct["name"]}"
                + "\n\n"
                + f"tool description: {async_tool_struct["description"]}"
                + "\n\n"
            )
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

    # defining mitigation agent
    mitigation_agent = MitigationAgent(
        llm=get_llm_backend_for_tools(),
        max_step=mitigation_agent_max_step,
        sync_tools=mitigation_agent_sync_tools,
        async_tools=mitigation_agent_async_tools,
        submit_tool=submit_tool,
        tool_descs=mitigation_agent_tool_descriptions,
    )
    mitigation_agent.build_agent()
    return mitigation_agent, mitigation_agent_prompt_path, mitigation_agent_max_step


def generate_run_summary(last_state: StateSnapshot, summary_system_prompt) -> str:
    """
    Returns a SystemMessage and a HumanMessage as a list. They are summaries and reflections of a given last run
    `last_state`.
    Ideally, we only need to summarize the last 20 (or all of them if less than 20) messages from the agent

        Args:
            last_state (State): the state from last run
        Returns:
            a list of SystemMessage and HumanMessage representing the reflections
    """
    llm = get_llm_backend_for_tools()
    logger.info("asking LLM to summarize and reflect last run")
    last_run_msgs = last_state.values.get("messages", None)
    summary_input_messages = [
        SystemMessage(summary_system_prompt),
        HumanMessage(f"Here are the list of messages happened in the last conversation. \n\n {last_run_msgs}"),
    ]
    if last_run_msgs is None:
        raise RuntimeError("StateSnapshot must contain messages!")
    res = llm.inference(summary_input_messages)
    res = res.content
    return res


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
