import asyncio
import logging
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END, START

from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools
from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_agent.state import State
from clients.stratus.stratus_utils.get_logger import get_logger
from clients.stratus.stratus_utils.get_starting_prompt import get_starting_prompts
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from clients.stratus.tools.stratus_tool_node import StratusToolNode

logger = get_logger()


class DiagnosisAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_node = None
        self.max_step = kwargs.get("max_step", 20)
        self.thinking_node = "thinking_step"
        self.tool_calling_node = "tool_calling_step"
        self.process_tool_call_node = "process_tool_call"
        self.post_round_process_node = "post_round_process"

    def should_submit_router(self, state: State):
        should_submit = state["num_steps"] == self.max_step and state["submitted"] == False
        logger.info("Should the agent submit?" + "Yes!" if should_submit else "No!")
        return self.force_submit_node if should_submit else self.post_round_process_node

    def post_round_process(self, state: State):
        logger.info("agent finished a round")
        logger.info("currently only incrementing step")
        return {
            "num_steps": state["num_steps"] + 1,
        }

    def build_agent(self):
        self.tool_node = StratusToolNode(async_tools=self.async_tools, sync_tools=self.sync_tools)

        # we add the node to the graph
        self.graph_builder.add_node(self.thinking_node, self.llm_thinking_step)
        self.graph_builder.add_node(self.tool_calling_node, self.llm_tool_call_step)
        self.graph_builder.add_node(self.process_tool_call_node, self.tool_node)
        self.graph_builder.add_node(self.post_round_process_node, self.post_round_process)
        self.graph_builder.add_node(self.force_submit_node, self.llm_force_submit_tool_call_node)

        # commenting these out first, focusing on basic diagnosis capability
        # self.graph_builder.add_node("post_tool_hook", self.post_tool_hook)
        # self.graph_builder.add_node("summarize_messages", self.summarize_messages)

        self.graph_builder.add_edge(START, self.thinking_node)
        self.graph_builder.add_edge(self.thinking_node, self.tool_calling_node)
        self.graph_builder.add_edge(self.tool_calling_node, self.process_tool_call_node)
        self.graph_builder.add_edge(self.process_tool_call_node, self.post_round_process_node)
        self.graph_builder.add_conditional_edges(
            self.process_tool_call_node,
            self.should_submit_router,
            {
                self.force_submit_node: self.force_submit_node,
                self.post_round_process_node: self.post_round_process_node,
            },
        )
        self.graph_builder.add_edge(self.force_submit_node, END)
        self.graph_builder.add_edge(self.post_round_process_node, END)

        #     self.graph_builder.add_conditional_edges(
        #     "explanation_agent",
        #     self.check_if_summaries_needed,  # This must return True or False
        #     {
        #         True: "summarize_messages",
        #         False: "tool_node",
        #     }
        # )
        # self.graph_builder.add_edge("summarize_messages", "explanation_agent")
        # self.graph_builder.add_conditional_edges(
        #     "tool_node",
        #     self.check_if_summaries_needed,  # should return True or False
        #     {True: "summarize_messages", False: "post_tool_hook"},
        # )
        # self.graph_builder.add_conditional_edges(
        #     "post_tool_hook",
        #     self.post_tool_route,
        #     {"explanation_agent": "explanation_agent", END: END},
        # )
        # self.graph_builder.add_edge("summarize_messages", "post_tool_hook")
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
            if last_state.values["num_steps"] == 20:
                logger.info("Agent did not converge.")
                break

        return graph_events[-1]


def main():
    file_parent_dir = Path(__file__).resolve().parent
    diagnosis_agent_config_path = file_parent_dir.parent / "configs" / "diagnosis_agent_config.yaml"
    diagnosis_agent_config = yaml.safe_load(open(diagnosis_agent_config_path, "r"))
    max_step = diagnosis_agent_config["max_step"]
    prompt_path = file_parent_dir.parent / "configs" / diagnosis_agent_config["prompts_path"]
    sync_tools = []
    async_tools = []
    tool_descriptions = ""
    for sync_tool_struct, async_tool_struct in zip(
        diagnosis_agent_config["sync_tools"], diagnosis_agent_config["async_tools"]
    ):
        sync_tools.append(str_to_tool(sync_tool_struct))
        async_tools.append(str_to_tool(async_tool_struct))
        tool_descriptions += sync_tool_struct["description"] + "\n\n" + async_tool_struct["description"]
    submit_tool = str_to_tool("submit_tool")

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

    res = asyncio.run(agent.arun(get_starting_prompts(prompt_path, max_step=max_step)))
    print(res)


if __name__ == "__main__":
    main()
