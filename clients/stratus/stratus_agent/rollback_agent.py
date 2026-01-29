import asyncio
import logging
from pathlib import Path

import yaml
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END
from langgraph.graph import START

from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_utils.get_starting_prompt import get_starting_prompts
from clients.stratus.stratus_utils.str_to_tool import str_to_tool
from clients.stratus.tools.stratus_tool_node import StratusToolNode
from llm_backend.init_backend import get_llm_backend_for_tools

logger = logging.getLogger("all.stratus.rollback")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class RollbackAgent(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_node = None
        self.loop_count = 0
        self.logger = logging.getLogger("all.stratus.rollback")

    def build_agent(self):
        self.tool_node = StratusToolNode(
            async_tools=self.async_tools,
            sync_tools=self.sync_tools,
        )

        self.graph_builder.add_node(self.thinking_prompt_inject_node, self.llm_thinking_prompt_inject_step)
        self.graph_builder.add_node(self.tool_calling_prompt_inject_node, self.llm_tool_call_prompt_inject_step)
        self.graph_builder.add_node(self.thinking_node, self.llm_thinking_step)
        self.graph_builder.add_node(self.tool_calling_node, self.llm_tool_call_step)
        self.graph_builder.add_node(self.process_tool_call_node, self.tool_node)
        self.graph_builder.add_node(self.post_round_process_node, self.post_round_process)
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
                self.force_submit_tool_call_node: self.force_submit_tool_call_node,
                self.post_round_process_node: self.post_round_process_node,
            },
        )
        self.graph_builder.add_edge(self.force_submit_tool_call_node, END)
        self.graph_builder.add_edge(self.post_round_process_node, END)

        self.memory_saver = MemorySaver()
        self.graph = self.graph_builder.compile(checkpointer=self.memory_saver)


async def main():
    file_parent_dir = Path(__file__).resolve().parent
    rollback_agent_config_path = file_parent_dir.parent / "configs" / "rollback_agent_config.yaml"
    rollback_agent_config = yaml.safe_load(open(rollback_agent_config_path, "r"))
    max_step = rollback_agent_config["max_step"]
    prompt_path = file_parent_dir.parent / "configs" / rollback_agent_config["prompts_path"]
    sync_tools = []
    async_tools = []
    tool_descriptions = ""
    if rollback_agent_config["sync_tools"] is not None:
        for sync_tool_struct in rollback_agent_config["sync_tools"]:
            sync_tools.append(str_to_tool(sync_tool_struct))
            tool_descriptions += (
                f"tool name: {sync_tool_struct["name"]}"
                + "\n\n"
                + f"tool descriptions {sync_tool_struct["description"]}"
                + "\n\n"
            )
    else:
        sync_tools = None
    if rollback_agent_config["async_tools"] is not None:
        for async_tool_struct in rollback_agent_config["async_tools"]:
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

    agent = RollbackAgent(
        llm=get_llm_backend_for_tools(),
        max_step=max_step,
        sync_tools=sync_tools,
        async_tools=async_tools,
        submit_tool=submit_tool,
        tool_descs=tool_descriptions,
    )
    agent.build_agent()

    last_state, graph_events = await agent.arun(get_starting_prompts(prompt_path, max_step=max_step))
    agent.clear_memory()
    return agent, last_state, graph_events


if __name__ == "__main__":
    asyncio.run(main())
