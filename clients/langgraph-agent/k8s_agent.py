import asyncio
import json
import logging
from typing import Annotated

from azure.mgmt.core.exceptions import TypedErrorInfo
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from llm_backend.init_backend import get_llm_backend_for_tools
from tools.jaeger_tools import *
from typing_extensions import TypedDict

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class State(TypedDict):
    # Messages have the type "list". The `add_messages` function
    # in the annotation defines how this state key should be updated
    # (in this case, it appends messages to the list, rather than overwriting them)
    messages: Annotated[list, add_messages]


llm = get_llm_backend_for_tools()
get_traces = GetTraces()
get_services = GetServices()
get_operations = GetOperations()
tools = [
    get_traces,
    get_services,
    get_operations,
]


def agent(state: State):
    return {"messages": [llm.inference(messages=state["messages"], tools=tools)]}


def route_tools(state: State):
    """
    Use in the conditional_edge to route to the ToolNode if the last message
    has tool calls. Otherwise, route to the end.
    """
    logger.info(f"route_tools: {state}")
    if isinstance(state, list):
        ai_message = state[-1]
    elif messages := state.get("messages", []):
        ai_message = messages[-1]
    else:
        raise ValueError(f"No messages found in input state to tool_edge: {state}")
    if hasattr(ai_message, "tool_calls") and len(ai_message.tool_calls) > 0:
        logger.info("invoking tool node: observability_tool_node")
        return "observability_tool_node"
    logger.info("invoking node: end")
    return END


graph_builder = StateGraph(State)
graph_builder.add_node("agent", agent)


class BasicToolNode:
    """A node that runs the tools requested in the last AIMessage."""

    def __init__(self, node_tools: list[BaseTool]) -> None:
        self.tools_by_name = {t.name: t for t in node_tools}

    def __call__(self, inputs: dict):
        if messages := inputs.get("messages", []):
            message = messages[-1]
        else:
            raise ValueError("No message found in input")
        logger.info(f"BasicToolNode: {message}")
        outputs = []
        for tool_call in message.tool_calls:
            # choosing the right ctx by tools called
            # assuming we have different mcp servers for different tools
            logger.info(f"invoking tool: {tool_call["name"]}, tool_call: {tool_call}")
            tool_result = asyncio.run(
                self.tools_by_name[tool_call["name"]].ainvoke(tool_call["args"])
            )
            logger.info(f"tool_result: {tool_result}")
            outputs.append(
                ToolMessage(
                    content=json.dumps(tool_result),
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"],
                )
            )
        return {"messages": outputs}


observability_tool_node = BasicToolNode(tools)
graph_builder.add_node("observability_tool_node", observability_tool_node)
graph_builder.add_edge(START, "agent")
# agent -> ob tool -> agent (loop)
# agent -> end
graph_builder.add_conditional_edges(
    "agent",
    route_tools,
    # The following dictionary lets you tell the graph to interpret the condition's outputs as a specific node
    # It defaults to the identity function, but if you
    # want to use a node named something else apart from "tools",
    # You can update the value of the dictionary to something else
    # e.g., "tools": "my_tools"
    {"observability_tool_node": "observability_tool_node", END: END},
)
graph_builder.add_edge("observability_tool_node", "agent")
memory = MemorySaver()
graph = graph_builder.compile(checkpointer=memory)
config = {"configurable": {"thread_id": "1"}}


def stream_graph_updates(user_input: str):
    for event in graph.stream(
        {"messages": [{"role": "user", "content": user_input}]},
        config=config,
        stream_mode="values",
    ):
        logger.info(event)
        event["messages"][-1].pretty_print()
        for value in event.values():
            try:
                logger.info("Assistant: %s", value["messages"][-1].content)
            except TypeError as e:
                logger.info(f"Error: {e}")


while True:
    try:
        user_input = input("User: ")
        if user_input.lower() in ["quit", "exit", "q"]:
            print("Goodbye!")
            break
        stream_graph_updates(user_input)
    except:
        # fallback if input() is not available
        break
