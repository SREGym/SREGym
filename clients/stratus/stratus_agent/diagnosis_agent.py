import logging
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END, START

from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from clients.stratus.tools.stratus_tool_node import StratusToolNode
from llm_backend.init_backend import get_llm_backend_for_tools

logger = logging.getLogger("all.stratus.diagnosis")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class DiagnosisAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_node = None
        self.max_step = kwargs.get("max_step", 20)
        self.loop_count = 0
        self.logger = logging.getLogger("all.stratus.diagnosis")

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

        all_init_prompts = ""
        for prompt in starting_prompts:
            all_init_prompts += prompt.content + "\n"

        graph_events = []

        while True:
            graph_config = {"configurable": {"thread_id": "1"}}

            logger.info(f"{'-' * 20} [Loop {self.loop_count}] {'-' * 20}")
            last_state = self.graph.get_state(config=graph_config)
            # logger.info("last state: %s", last_state)
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
                if (not graph_events) or event["messages"][-1] != graph_events[-1]["messages"][-1]:
                    # print(f"Last message: {graph_events[-1]['messages']}")
                    event["messages"][-1].pretty_print()
                graph_events.append(event)
            last_state = self.graph.get_state(config=graph_config)
            if last_state.values["submitted"]:
                logger.info(f"[Loop {self.loop_count}] Agent submitted, breaking loop.")
                # Ensure the final state is included in graph_events
                if not graph_events or last_state.values["messages"][-1] != graph_events[-1]["messages"][-1]:
                    graph_events.append(last_state.values)
                break

            self.loop_count += 1

            # print(f"================{last_state.values['num_steps']}===============")

        return last_state, graph_events


def build_default_diagnosis_agent():
    file_parent_dir = Path(__file__).resolve().parent
    diagnosis_agent_config_path = file_parent_dir.parent / "configs" / "diagnosis_agent_config.yaml"
    diagnosis_agent_config = yaml.safe_load(open(diagnosis_agent_config_path, "r"))
    max_step = diagnosis_agent_config["max_step"]
    prompt_path = file_parent_dir.parent / "configs" / diagnosis_agent_config["prompts_path"]
    sync_tools = []
    async_tools = []
    tool_descriptions = ""
    if diagnosis_agent_config["sync_tools"] is not None:
        for sync_tool_struct in diagnosis_agent_config["sync_tools"]:
            sync_tools.append(str_to_tool(sync_tool_struct))
            tool_descriptions += (
                f"tool name: {sync_tool_struct["name"]}"
                + "\n\n"
                + f"tool descriptions {sync_tool_struct["description"]}"
                + "\n\n"
            )
    else:
        sync_tools = None
    if diagnosis_agent_config["async_tools"] is not None:
        for async_tool_struct in diagnosis_agent_config["async_tools"]:
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
                        ans (str): the answer you would like to submit to the benchmark
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
    return agent, prompt_path, max_step


async def single_run_with_predefined_prompts(init_prompts):
    agent, prompt_path, max_step = build_default_diagnosis_agent()
    last_state, graph_events = await agent.arun(init_prompts)
    logger.info("Clearing agent's memory")
    agent.clear_memory()
    return agent, last_state, graph_events
