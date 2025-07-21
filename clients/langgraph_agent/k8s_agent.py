import asyncio
import json
import os
import logging

import yaml
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode

from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools
from clients.langgraph_agent.state import State
from clients.langgraph_agent.tools.basic_tool_node import BasicToolNode
from clients.langgraph_agent.tools.jaeger_tools import get_operations, get_services, get_traces
from clients.langgraph_agent.tools.prometheus_tools import get_metrics
from clients.langgraph_agent.tools.text_editing.file_manip import create, edit, goto_line, insert, open_file
from clients.langgraph_agent.tools.compile.compile_tool import compile_postgresql_server  

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ROOT_REPO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class XAgent:
    # agents are modelled as graphs in langgraph, where each node in the graph
    # represents a unit of work in the agent workflow.
    # e.g., querying traces, running kubectl get pods, etc.
    # it's completely up to us what we implement in each node,
    # we don't have to query the llm at each step.
    def __init__(self, llm):
        self.graph_builder = StateGraph(State)
        self.graph = None
        # get_traces_tool = StructuredTool.from_function(
        #     name="get_traces",
        #     func=lambda x: "Not implemented sync version of tool",
        #     coroutine=get_traces,
        #     description="get_traces",
        #     args_schema=GetTracesInput,
        # )
        self.observability_tools = [get_traces, get_services, get_operations, get_metrics]

        self.file_editing_tools = [open_file, goto_line, create, edit, insert]

        self.compile_tools = [compile_postgresql_server]
        self.llm = llm

        # here are testing purposes attr
        self.test_campaign_file = ""
        self.test_tool_or_ai_response = "tool"
        self.test_tool_call_idx = 0

    def test_campaign_setter(self, test_campaign_file):
        self.test_campaign_file = test_campaign_file

    @property
    def all_tools(self):
        return [*self.observability_tools, *self.file_editing_tools, *self.compile_tools]

    def route_tools(self, state: State):
        """
        Use in the conditional_edge to route to the ToolNode if the last message
        has tool calls. Otherwise, route to the end.
        """
        print(f"[route tools] in route tools: {state}")
        logger.info("in route tools: %s", state)
        file_tool_names = ["open_file", "goto_line", "create", "edit", "insert"]
        observability_tool_names = ["get_traces", "get_services", "get_operations", "get_metrics"]
        compile_tool_names = ["compile_postgresql_server"]
        if isinstance(state, list):
            ai_message = state[-1]
        elif messages := state.get("messages", []):
            ai_message = messages[-1]
        else:
            raise ValueError(f"No messages found in input state to tool_edge: {state}")
        if hasattr(ai_message, "tool_calls") and len(ai_message.tool_calls) > 0:
            tool_name = ai_message.tool_calls[0]["name"]
            logger.info("routing tool name: %s", tool_name)
            if tool_name in file_tool_names:
                logger.info("invoking tool node: file tool")
                return "file_editing_tool_node"
            elif tool_name in compile_tool_names:
                logger.info("invoking tool node: compile tool")
                return "compile_tool_node"
            elif tool_name in observability_tool_names:
                logger.info("invoking tool node: observability tool")
                return "observability_tool_node"
            else:
                logger.info("invoking tool node: end")
                return END
        print("no tool call, returning END")
        logger.info("no tool call, returning END")
        return END

    def mock_llm_inference_step(self, state: State):
        print(f"[mock llm] called by graph, currently on {self.test_tool_call_idx}th tool call")
        ai_message_template = AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call_osNIUg8kE7psP360dHinqNbm",
                        "function": {
                            "arguments": "",
                            "name": "",
                        },
                        "type": "function",
                    }
                ],
                "refusal": None,
            },
            response_metadata={
                "token_usage": {
                    "completion_tokens": 39,
                    "prompt_tokens": 588,
                    "total_tokens": 627,
                    "completion_tokens_details": {
                        "accepted_prediction_tokens": 0,
                        "audio_tokens": 0,
                        "reasoning_tokens": 0,
                        "rejected_prediction_tokens": 0,
                    },
                    "prompt_tokens_details": {"audio_tokens": 0, "cached_tokens": 0},
                },
                "model_name": "gpt-4o-2024-08-06",
                "system_fingerprint": "fp_07871e2ad8",
                "service_tier": "default",
                "finish_reason": "tool_calls",
                "logprobs": None,
            },
            tool_calls=[
                {
                    "name": "",
                    "args": {
                        "path": "",
                        "line_number": "",
                    },
                    "id": "call_osNIUg8kE7psP360dHinqNbm",
                    "type": "tool_call",
                }
            ],
            usage_metadata={
                "input_tokens": 588,
                "output_tokens": 39,
                "total_tokens": 627,
                "input_token_details": {"audio": 0, "cache_read": 0},
                "output_token_details": {"audio": 0, "reasoning": 0},
            },
        )

        logger.info("invoking mock llm inference, custom state: %s", state)
        print(f"[mock llm] msg branch: {self.test_tool_or_ai_response}")
        test_campaign = yaml.safe_load(open(self.test_campaign_file, "r"))
        print(f"[mock llm] test campaign tool calls: {test_campaign['tool_calls']}")
        if self.test_tool_or_ai_response == "tool":
            tool_call = test_campaign["tool_calls"][self.test_tool_call_idx]
            function_name = tool_call["name"]
            function_args = {key: value for key, value in tool_call.items() if key != "name"}
            for key, value in function_args.items():
                if key == "path":
                    function_args[key] = ROOT_REPO_PATH + "/" + value
            function_args_str = json.dumps(function_args)
            ai_message_template.additional_kwargs["tool_calls"][0]["function"]["arguments"] = function_args_str
            ai_message_template.additional_kwargs["tool_calls"][0]["function"]["name"] = function_name
            ai_message_template.tool_calls[0]["name"] = function_name
            ai_message_template.tool_calls[0]["args"] = function_args
            logger.info("[mock llm] type: %s, ai message returned: %s", type(ai_message_template), ai_message_template)
            print(f"[mock llm] tool calling, returning to ai")
            logger.info("[mock llm] tool calling, returning to ai")
            self.test_tool_call_idx += 1
        elif self.test_tool_or_ai_response == "ai":
            ai_message_template.tool_calls = []
            ai_message_template.content = "test"
            ai_message_template.additional_kwargs = {"refusal": None}
            ai_message_template.response_metadata["finish_reason"] = "stop"
            print(f"[mock llm] ai messaging, returning to tool")
            logger.info("[mock llm] ai messaging, returning to tool")

        if self.test_tool_or_ai_response == "tool":
            self.test_tool_or_ai_response = "ai"
        elif self.test_tool_or_ai_response == "ai":
            self.test_tool_or_ai_response = "tool"

        logger.info(
            "[mock llm] next msg branch: %s, messages returns: %s",
            self.test_tool_or_ai_response,
            state["messages"] + [ai_message_template],
        )
        print(f"[mock llm] next msg branch: {self.test_tool_or_ai_response}")
        print(f"[mock llm] messages returns: {state["messages"] + [ai_message_template]}")
        output = [*state["messages"], ai_message_template]
        return {
            "messages": output,
            "curr_file": state["curr_file"],
            "curr_line": state["curr_line"],
            "workdir": state["workdir"],
        }

    # this is the agent node. it simply queries the llm and return the results
    def llm_inference_step(self, state: State):
        logger.info("invoking llm inference, custom state: %s", state)
        return {
            "messages": [self.llm.inference(messages=state["messages"], tools=self.all_tools)],
            "curr_file": state["curr_file"],
            "curr_line": state["curr_line"],
            "workdir": state["workdir"],
        }

    def build_agent(self, mock: bool = False):
        # we add the node to the graph
        if mock:
            self.graph_builder.add_node("agent", self.mock_llm_inference_step)
        else:
            self.graph_builder.add_node("agent", self.llm_inference_step)

        # we also have a tool node. this tool node connects to a jaeger MCP server
        # and allows you to query any jaeger information

        observability_tool_node = BasicToolNode(self.observability_tools, is_async=True)
        file_editing_tool_node = ToolNode(self.file_editing_tools)
        compile_tool_node = ToolNode(self.compile_tools)

        # we add the node to the graph
        self.graph_builder.add_node("observability_tool_node", observability_tool_node)
        self.graph_builder.add_node("file_editing_tool_node", file_editing_tool_node)
        self.graph_builder.add_node("compile_tool_node", compile_tool_node)

        # after creating the nodes, we now add the edges
        # the start of the graph is denoted by the keyword START, end is END.
        # here, we point START to the "agent" node
        self.graph_builder.add_edge(START, "agent")

        # once we arrive at the "agent" node, the execution graph can
        # have 2 paths: either choosing to use a tool or not.
        # e.g.,
        # agent -> ob tool -> agent -> ob tool (tool loop)
        # agent -> agent -> agent -> end (normal chatbot loop)
        # this is accomplished by "conditional edges" in the graph
        # we implement "route_tools," which routes the execution based on the agent's
        # output. if the output is a tool usage, we direct the execution to the tool and loop back to the agent node
        # if not, we finish *one* graph traversal (i.e., to END)
        self.graph_builder.add_conditional_edges(
            "agent",
            self.route_tools,
            # The following dictionary lets you tell the graph to interpret the condition's outputs as a specific node
            # It text_editing to the identity function, but if you
            # want to use a node named something else apart from "tools",
            # You can update the value of the dictionary to something else
            # e.g., "tools": "my_tools"
            {
                "observability_tool_node": "observability_tool_node",
                "file_editing_tool_node": "file_editing_tool_node",
                "compile_tool_node": "compile_tool_node",
                END: END,
            },
        )
        # interestingly, for short-term memory (i.e., agent trajectories or conversation history), we need
        # to explicitly implement it.
        # here, it is implemented as a in-memory checkpointer.
        self.graph_builder.add_edge("observability_tool_node", "agent")
        self.graph_builder.add_edge("file_editing_tool_node", "agent")
        self.graph_builder.add_edge("compile_tool_node", "agent")
        memory = MemorySaver()
        self.graph = self.graph_builder.compile(checkpointer=memory)

    async def graph_step(self, user_input: str):
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")
        config = {"configurable": {"thread_id": "1"}}
        last_state = self.graph.get_state(config=config)
        logger.info("last state: %s", last_state)
        if len(last_state.values) != 0:
            logger.info("last state values: %s", last_state.values["messages"])
            msgs = last_state.values["messages"] + [{"role": "user", "content": user_input}]
            workdir = last_state.values["workdir"]
            curr_file = last_state.values["curr_file"]
            curr_line = last_state.values["curr_line"]
            logger.info("last curr_file: %s, last curr_line: %s, last messages: %s", curr_file, curr_line, msgs)
            state = {"messages": msgs, "curr_file": curr_file, "curr_line": curr_line}
        else:
            state = {
                "messages": [{"role": "user", "content": user_input}],
                "workdir": "",
                "curr_file": "",
                "curr_line": 0,
            }
        async for event in self.graph.astream(
            state,
            config=config,
            stream_mode="values",
        ):
            event["messages"][-1].pretty_print()

    def save_agent_graph_to_png(self):
        with open("./agent_graph.png", "wb") as png:
            png.write(self.graph.get_graph().draw_mermaid_png())


async def main():
    llm = get_llm_backend_for_tools()
    xagent = XAgent(llm)
    xagent.build_agent()
    xagent.save_agent_graph_to_png()
    while True:
        user_input = input("User: ")
        if user_input.lower() in ["quit", "exit", "q"]:
            print("Goodbye!")
            break
        await xagent.graph_step(user_input)


if __name__ == "__main__":
    asyncio.run(main())
