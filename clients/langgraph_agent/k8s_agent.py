from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode

from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools
from clients.langgraph_agent.state import State
from clients.langgraph_agent.tools.basic_tool_node import BasicToolNode
from clients.langgraph_agent.tools.jaeger_tools import *
from clients.langgraph_agent.tools.text_editing.file_manip import create, edit, goto_line, insert, open_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class XAgent:
    # agents are modelled as graphs in langgraph, where each node in the graph
    # represents a unit of work in the agent workflow.
    # e.g., querying traces, running kubectl get pods, etc.
    # it's completely up to us what we implement in each node,
    # we don't have to query the llm at each step.
    def __init__(self, llm):
        self.graph_builder = StateGraph(State)
        self.graph = None

        get_traces = GetTraces()
        get_services = GetServices()
        get_operations = GetOperations()
        self.observability_tools = [
            get_traces,
            get_services,
            get_operations,
        ]
        self.file_editing_tools = [open_file, goto_line, create, edit, insert]
        self.llm = llm

    @property
    def all_tools(self):
        return [*self.observability_tools, *self.file_editing_tools]

    def route_tools(self, state: State):
        """
        Use in the conditional_edge to route to the ToolNode if the last message
        has tool calls. Otherwise, route to the end.
        """
        file_tool_names = ["open_file", "goto_line", "create", "edit", "insert"]
        observability_tool_names = ["get_traces", "get_services", "get_operations"]
        if isinstance(state, list):
            ai_message = state[-1]
        elif messages := state.get("messages", []):
            ai_message = messages[-1]
        else:
            raise ValueError(f"No messages found in input state to tool_edge: {state}")
        if hasattr(ai_message, "tool_calls") and len(ai_message.tool_calls) > 0:
            tool_name = ai_message.tool_calls[0]["name"]
            if tool_name in file_tool_names:
                logger.info("invoking tool node: file tool")
                return "file_editing_tool_node"
            elif tool_name in observability_tool_names:
                logger.info("invoking tool node: observability tool")
                return "observability_tool_node"
            else:
                logger.info("invoking tool node: end")
                return END
        logger.info("no tool call, returning END")
        return END

    # this is the agent node. it simply queries the llm and return the results
    def llm_inference_step(self, state: State):
        logger.info("invoking llm inference, custom state: %s", state)
        return {
            "messages": [self.llm.inference(messages=state["messages"], tools=self.all_tools)],
            "curr_file": state["curr_file"],
            "curr_line": state["curr_line"],
        }

    def build_agent(self):
        # we add the node to the graph
        self.graph_builder.add_node("agent", self.llm_inference_step)

        # we also have a tool node. this tool node connects to a jaeger MCP server
        # and allows you to query any jaeger information

        observability_tool_node = BasicToolNode(self.observability_tools, is_async=True)
        file_editing_tool_node = ToolNode(self.file_editing_tools)

        # we add the node to the graph
        self.graph_builder.add_node("observability_tool_node", observability_tool_node)
        self.graph_builder.add_node("file_editing_tool_node", file_editing_tool_node)

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
                END: END,
            },
        )
        # interestingly, for short-term memory (i.e., agent trajectories or conversation history), we need
        # to explicitly implement it.
        # here, it is implemented as a in-memory checkpointer.
        self.graph_builder.add_edge("observability_tool_node", "agent")
        self.graph_builder.add_edge("file_editing_tool_node", "agent")
        memory = MemorySaver()
        self.graph = self.graph_builder.compile(checkpointer=memory)

    def graph_step(self, user_input: str):
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")
        config = {"configurable": {"thread_id": "1"}}
        last_state = self.graph.get_state(config=config)
        logger.info("last state: %s", last_state)
        if len(last_state.values) != 0:
            msgs = last_state.values["messages"] + [{"role": "user", "content": user_input}]
            curr_file = last_state.values["curr_file"]
            curr_line = last_state.values["curr_line"]
            logger.info("last curr_file: %s, last curr_line: %s, last messages: %s", curr_file, curr_line, msgs)
            state = {"messages": msgs, "curr_file": curr_file, "curr_line": curr_line}
        else:
            state = {"messages": [{"role": "user", "content": user_input}], "curr_file": "", "curr_line": 0}
        for event in self.graph.stream(
            state,
            config=config,
            stream_mode="values",
        ):
            event["messages"][-1].pretty_print()
            for value in event.values():
                try:
                    logger.info("Assistant: %s", value["messages"][-1].content)
                except TypeError as e:
                    pass

    def save_agent_graph_to_png(self):
        with open("./agent_graph.png", "wb") as png:
            png.write(self.graph.get_graph().draw_mermaid_png())


if __name__ == "__main__":
    llm = get_llm_backend_for_tools()
    xagent = XAgent(llm)
    xagent.build_agent()
    xagent.save_agent_graph_to_png()
    # a short chatbot loop to demonstrate the workflow.
    # TODO: make a real file-editing agent to test both state & memory mgmt and file editing tools
    while True:
        user_input = input("User: ")
        if user_input.lower() in ["quit", "exit", "q"]:
            print("Goodbye!")
            break
        xagent.graph_step(user_input)
