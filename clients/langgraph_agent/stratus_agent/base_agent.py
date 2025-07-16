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
        self.max_tool_call = config.max_tool_call
        self.prompts_file_path = config.prompts_file_path
        self.async_tools = config.async_tools
        self.sync_tools = config.sync_tools
        self.llm = llm

        # self.llm = llm.bind_tools(self.sync_tools + self.async_tools, tool_choice="required")

    def llm_inference_step(self, state: State):
        logger.info(f"The agent has called tools {state['tool_calling_times']} times!")
        return {
            # "messages": [self.llm.invoke(state["messages"])]
            "messages": [self.llm.inference(messages=state["messages"],
                                            tools=self.async_tools + self.sync_tools)],
        }

    def post_tool_route(self, state: State):
        """
        Use in the conditional edge to route the path after node post_tool_hook.
        Route to END if tool calling quota is used up or the state's 'submitted' value
        is True; otherwise, route to the agent.
        """
        if state["tool_calling_times"] > self.max_tool_call or state["submitted"]:
            return END
        else:
            return "agent"

    def post_tool_hook(self, state: State):
        """Post-tool hook."""
        tool_calling_times = state["tool_calling_times"]
        # Limited times to call tools other than the submit tool
        if not state["submitted"]:
            tool_calling_times += 1

            if tool_calling_times > self.max_tool_call:
                sys_mes = f"You have reached to the limit of max tool calling. Will be forced to end."
                logger.info(sys_mes)
            else:
                if tool_calling_times < self.max_tool_call:
                    sys_mes = f"You have already called the tools {tool_calling_times} times. " \
                              f"You can still call the tools " \
                              f"{self.max_tool_call - tool_calling_times} more times."
                else:
                    sys_mes = f"You have already used up all your tool call attempts. " \
                              f"You should call the submit_tool and submit your answer. " \
                              f"If you keep calling other tools, the process will be forced to end " \
                              f"and you will be considered failing the tasks."
        else:
            sys_mes = f"Submission has been detected. Will be routed to END."

        # update messages and tool_calling_times of state
        return {"messages": [SystemMessage(sys_mes)],
                "tool_calling_times": tool_calling_times}

    def build_agent(self):
        tool_node = StratusToolNode(async_tools=self.async_tools,
                                    sync_tools=self.sync_tools)

        # we add the node to the graph
        self.graph_builder.add_node("agent", self.llm_inference_step)
        self.graph_builder.add_node("tool_node", tool_node)
        self.graph_builder.add_node("post_tool_hook", self.post_tool_hook)

        self.graph_builder.add_edge(START, "agent")
        self.graph_builder.add_edge("agent", "tool_node")
        self.graph_builder.add_edge("tool_node", "post_tool_hook")

        self.graph_builder.add_conditional_edges(
            "post_tool_hook",
            self.post_tool_route,
            {"agent": "agent", END: END},
        )

        self.graph = self.graph_builder.compile()

    def get_init_prompts(self, app_summary):
        with open(self.prompts_file_path, "r") as file:
            data = yaml.safe_load(file)
            sys_prompt = data["diagnosis_agent"]["system"].format(max_tool_call=self.max_tool_call,
                                                                  app_summary=app_summary)
            user_prompt = data["diagnosis_agent"]["user"]
            prompts = []
            if sys_prompt:
                prompts.append(SystemMessage(sys_prompt))
            if user_prompt:
                prompts.append(HumanMessage(user_prompt))
            return prompts

    def run(self, app_summary):
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")

        prompts = self.get_init_prompts(app_summary)
        if len(prompts) == 0:
            raise ValueError("No prompts used to start the conversation!")

        state = {
            "messages": prompts,
            "workdir": "",
            "curr_file": "",
            "curr_line": 0,
            "tool_calling_times": 0,
            "submitted": False,
            "ans": dict(),
        }

        return list(self.graph.stream(state,
                                      config={"recursion_limit": 100},
                                      stream_mode="values"))[-1]
