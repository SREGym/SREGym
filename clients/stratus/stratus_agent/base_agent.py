import logging
import os

import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.constants import END
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from clients.stratus.configs import BaseAgentCfg
from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools
from clients.stratus.stratus_agent.state import State
from clients.stratus.tools.stratus_tool_node import StratusToolNode

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class BaseAgent:
    def __init__(self, llm, max_step, prompt_path, sync_tools, async_tools):
        self.graph_builder = StateGraph(State)
        self.graph: CompiledStateGraph | None = None
        self.max_round = max_step
        self.prompts_file_path = prompt_path
        self.async_tools = async_tools
        self.sync_tools = sync_tools
        self.llm = llm

    def llm_inference_step(self, messages, tools):
        return self.llm.inference(messages=messages, tools=tools)

    def llm_thinking_step(self, state: State, tool_descs: str):
        human_prompt = HumanMessage(
            content="You are now in the thinking stage. Here are all the tools you can use:\n"
            + tool_descs
            + "Choose a tool from the list and output the tool name. Justify your tool choice. In the next step, you will generate a tool call for this tool"
        )
        # planning step, not providing tool
        ai_message = self.llm_inference_step(state["messages"] + [human_prompt], tools=[])
        # ai_message.additional_kwargs["is_thought"] = True
        # let's rely on the annotated dict type
        # new_messages = [human_prompt, ai_message]
        return {
            "messages": ai_message,
        }

    def save_agent_graph_to_png(self):
        with open("./agent_graph.png", "wb") as png:
            png.write(self.graph.get_graph().draw_mermaid_png())

    def run(self, starting_prompts):
        """Running an agent

        Args:
            starting_prompts (list[SystemMessage | HumanMessage]): The data inside the dict will be filled into the prompts.

        Returns:
            final state of the agent running, including messages and other state values.
        """
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")

        if len(starting_prompts) == 0:
            raise ValueError("No prompts used to start the conversation!")

        state = {
            "messages": starting_prompts,
            "num_steps": 0,
            "submitted": False,
        }

        return list(
            self.graph.stream(
                state,
                # recursion_limit could be as large as possible as we have our own limit.
                config={"recursion_limit": 10000},
                stream_mode="values",
            )
        )[-1]

    async def arun(self, starting_prompts):
        """
                Async running an agent

                Args:
                    starting_prompts (dict): The data inside the dict will be filled into the prompts.
        :w

                Returns:
                    final state of the agent running, including messages and other state values.
        """
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")

        if len(starting_prompts) == 0:
            raise ValueError("No prompts used to start the conversation!")

        state = {
            "messages": starting_prompts,
            "workdir": "",
            "curr_file": "",
            "curr_line": 0,
            "num_rounds": 0,
            "rec_submission_rounds": 0,
            "submit_tried": False,
            "submitted": False,
            "ans": dict(),
        }

        res = []
        async for event in self.graph.astream(
            state,
            # recursion_limit could be as large as possible as we have our own limit.
            config={"recursion_limit": 10000},
            stream_mode="values",
        ):
            res.append(event)
            event["messages"][-1].pretty_print()

        return res[-1]
