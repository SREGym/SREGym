import asyncio
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END, START

from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools
from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_utils.get_logger import get_logger
from clients.stratus.stratus_utils.get_starting_prompt import get_starting_prompts
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from clients.stratus.tools.stratus_tool_node import StratusToolNode

logger = get_logger()


class MitigationAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_node = None
        self.max_step = kwargs.get("max_step", 20)

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
        self.graph_builder.add_edge(self.force_submit_tool_call_node, END)
        self.graph_builder.add_edge(self.post_round_process_node, END)

        memory = MemorySaver()
        self.graph = self.graph_builder.compile(checkpointer=memory)

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

        graph_events = []
        while True:
            graph_config = {"configurable": {"thread_id": "1"}}
            last_state = self.graph.get_state(config=graph_config)
            # logger.info("last state: %s", last_state)
            if len(last_state.values) != 0:
                logger.info("There were last states.")
                # this is all the previous msgs the agent had, we just inherit them in the next graph traversal
                state = last_state.values
            else:
                logger.info("There were no states.")
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
                }

            async for event in self.graph.astream(
                state,
                # recursion_limit could be as large as possible as we have our own limit.
                config={"recursion_limit": 10000, "configurable": {"thread_id": "1"}},
                stream_mode="values",
            ):
                graph_events.append(event)
                event["messages"][-1].pretty_print()
            last_state = self.graph.get_state(config=graph_config)
            if last_state.values["submitted"]:
                logger.info("agent submitted, breaking loop.")
                break

        return last_state


def main():
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
            mitigation_agent_tool_descriptions += sync_tool_struct["description"] + "\n\n"
    else:
        mitigation_agent_sync_tools = None
    if mitigation_agent_config["async_tools"] is not None:
        for async_tool_struct in mitigation_agent_config["async_tools"]:
            mitigation_agent_async_tools.append(str_to_tool(async_tool_struct))
            mitigation_agent_tool_descriptions += async_tool_struct["description"] + "\n\n"
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
    mitigation_agent.save_agent_graph_to_png()

    res = asyncio.run(
        mitigation_agent.arun(get_starting_prompts(mitigation_agent_prompt_path, max_step=mitigation_agent_max_step))
    )
    print(res)


if __name__ == "__main__":
    main()
