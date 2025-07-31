import logging

import yaml
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.constants import END
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from clients.configs.stratus_config import BaseAgentCfg
from clients.langgraph_agent.state import State
from clients.langgraph_agent.tools.stratus_tool_node import StratusToolNode

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class BaseAgent:
    def __init__(self, llm, config: BaseAgentCfg):
        self.graph_builder = StateGraph(State)
        self.graph: CompiledStateGraph | None = None
        self.max_round = config.max_round
        self.max_rec_round = config.max_rec_round
        self.max_tool_call_one_round = config.max_tool_call_one_round
        self.prompts_file_path = config.prompts_file_path
        self.async_tools = config.async_tools
        self.sync_tools = config.sync_tools
        self.llm = llm

        # self.llm = llm.bind_tools(self.sync_tools + self.async_tools, tool_choice="required")

    def llm_inference_step(self, state: State):
        return {
            # "messages": [self.llm.invoke(state["messages"])]
            "messages": [self.llm.inference(messages=state["messages"],
                                            tools=self.async_tools + self.sync_tools)],
        }

    def llm_explanation_step(self, state: State):
        state["messages"].append(
            HumanMessage(content="You are now in explanation stage; "
                                 "please briefly explain why you want to call the tools with the arguments next; "
                                 "the tools you mentioned must be available to you at first.")
        )
        return self.llm_inference_step(state)

    def llm_tool_call_step(self, state: State):
        state["messages"].append(
            HumanMessage(content="You are now in tool-call stage; "
                                 "please make tool calls consistent with your explanation")
        )
        return self.llm_inference_step(state)

    def post_tool_route(self, state: State):
        """
        Use in the conditional edge to route the path after node post_tool_hook.
        Route to END if tool calling quota is used up or the state's 'submitted' value
        is True; otherwise, route to the agent.
        """
        if state["submitted"] or \
                state["num_rounds"] > self.max_round or \
                state["rec_submission_rounds"] > self.max_rec_round:
            return END
        else:
            return "agent"

    def post_tool_hook(self, state: State):
        """Post-tool hook."""
        num_rounds = state["num_rounds"]
        rec_submission_rounds = state["rec_submission_rounds"]
        # Limited times to call tools other than the submit tool
        if not state["submitted"]:
            if state["submit_tried"]:
                # information for agent to rectify its args passed to the submit_tool
                rec_submission_rounds += 1
                if rec_submission_rounds == 1:
                    sys_mes = f"You have already tried to submit your answer with the submit_tool, " \
                              f"but failed calling it. Possible reasons are the arguments you use " \
                              f"do not match the signature of the tool. You'll have at most {self.max_rec_round} " \
                              f"rounds to rectify the args passed to the submit_tool, during which you " \
                              f"are only allowed to call the submit_tool."
                else:
                    sys_mes = f"Fail calling submit_tool again; {self.max_rec_round - rec_submission_rounds + 1} " \
                              f"more rounds left for rectification."
            else:
                num_rounds += 1

                if num_rounds > self.max_round:
                    sys_mes = f"You have reached to the limit of max number of rounds. Will be forced to end."
                    logger.info(sys_mes)
                else:
                    if num_rounds < self.max_round:
                        sys_mes = f"You have already ran {num_rounds} rounds. " \
                                  f"You can still run " \
                                  f"{self.max_round - num_rounds} more rounds."
                    else:
                        sys_mes = f"You have already reached the limit of max number of rounds. " \
                                  f"You should call the submit_tool and submit your answer in the " \
                                  f"tool-call stage of next round. " \
                                  f"If you keep calling other tools, the process will be forced to end " \
                                  f"and you will be considered failing the tasks."
        else:
            sys_mes = f"Submission has been detected. Will be routed to END."

        # update messages and num_rounds of state
        return {"messages": [SystemMessage(sys_mes)],
                "num_rounds": num_rounds,
                "rec_submission_rounds": rec_submission_rounds}

    def build_agent(self):
        tool_node = StratusToolNode(async_tools=self.async_tools,
                                    sync_tools=self.sync_tools,
                                    max_tool_call_one_round=self.max_tool_call_one_round)

        # we add the node to the graph
        self.graph_builder.add_node("explanation_agent", self.llm_explanation_step)
        self.graph_builder.add_node("tool_agent", self.llm_tool_call_step)
        self.graph_builder.add_node("tool_node", tool_node)
        self.graph_builder.add_node("post_tool_hook", self.post_tool_hook)

        self.graph_builder.add_edge(START, "explanation_agent")
        self.graph_builder.add_edge("explanation_agent", "tool_agent")
        self.graph_builder.add_edge("tool_agent", "tool_node")
        self.graph_builder.add_edge("tool_node", "post_tool_hook")

        self.graph_builder.add_conditional_edges(
            "post_tool_hook",
            self.post_tool_route,
            {"agent": "explanation_agent", END: END},
        )

        self.graph = self.graph_builder.compile()

    def get_init_prompts(self, data_for_prompts: dict):
        data_for_prompts["max_round"] = self.max_round
        with open(self.prompts_file_path, "r") as file:
            data = yaml.safe_load(file)
            sys_prompt = data["system"].format(**data_for_prompts)
            user_prompt = data["user"].format(**data_for_prompts)
            prompts = []
            if sys_prompt:
                prompts.append(SystemMessage(sys_prompt))
            if user_prompt:
                prompts.append(HumanMessage(user_prompt))
            return prompts

    def run(self, data_for_prompts: dict):
        """Running an agent

        Args:
            data_for_prompts (dict): The data inside the dict will be filled into the prompts.

        Returns:
            final state of the agent running, including messages and other state values.
        """
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")

        prompts = self.get_init_prompts(data_for_prompts)
        if len(prompts) == 0:
            raise ValueError("No prompts used to start the conversation!")

        state = {
            "messages": prompts,
            "workdir": "",
            "curr_file": "",
            "curr_line": 0,
            "num_rounds": 0,
            "rec_submission_rounds": 0,
            "submit_tried": False,
            "submitted": False,
            "ans": dict(),
        }

        return list(self.graph.stream(state,
                                      # recursion_limit could be as large as possible as we have our own limit.
                                      config={"recursion_limit": 10000},
                                      stream_mode="values"))[-1]
