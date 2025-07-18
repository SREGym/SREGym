import logging
import yaml
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langgraph.constants import END
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools

import os
from collections import Counter

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
    
    def check_if_summaries_needed(self, state: State):
        """ Check if summaries are needed based on the number of messages."""
        messages = state["messages"]
        tool_calls = state["tool_calling_times"]

        logger.info("Checking if summaries are needed, current messages: %s", messages)
        logger.info("Number of tool calls: %d", tool_calls)

        if tool_calls >=3  and tool_calls % 3 == 0:
            logger.info("Summaries are needed, multiple of 10 messages.")
            return True
        else:
            logger.info("No summaries needed")


            return False
   
    
    def summarize_messages(self, state: State):
        """ Summarize the messages in the conversation history."""
        messages = [msg for msg in state["messages"] if not (isinstance(msg, AIMessage) and msg.additional_kwargs.get("is_summary"))][-(int(os.environ["SUMMARY_FREQUENCY"])-1):]
        def format_messages(msgs):
            formatted = ""
            for msg in msgs:
                # Skip summary messages
                if isinstance(msg, AIMessage) and msg.additional_kwargs.get("is_summary", False):
                    continue
                if isinstance(msg, (AIMessage, HumanMessage)):
                    role = "Ai" if isinstance(msg, AIMessage) else "Human"
                    formatted += f"{role}: {msg.content}\n"
            return formatted
        

            return type_counts
        logger.info("Summarizing messages: %s", messages)
        # Count the number of messages of each type
        formatted_history = format_messages(messages)
        logger.info("Formatted conversation history: %s", formatted_history)
        summary_prompt = [
    SystemMessage(content="You are a helpful assistant that summarizes conversations."),
    HumanMessage(content="""
Summarize the following conversation history in concise bullet points.
At the end, add a final line beginning with 'Answer:' that gives the AI's most recent reply.

Format:
- [bullet point]
- ...
Answer: [final AI reply]

Conversation:
""" + formatted_history)
]
        llm = get_llm_backend_for_tools()    

        messages_summary = llm.inference(messages=summary_prompt)

# If the response is an AIMessage or similar, extract `.content`
        if isinstance(messages_summary, AIMessage):
            summary_content = messages_summary.content
        else:
            summary_content = str(messages_summary)
        answer = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and not msg.additional_kwargs.get("is_summary", False):
                answer = msg.content.strip()
            break
        # Format the summary content
        lines = summary_content.strip().split("\n")
        formatted_summary_lines = []

        for line in lines:
            clean_line = line.strip().lstrip("-").strip()
            # Filter out any existing "Answer:" lines
            if clean_line.lower().startswith("answer:"):
                continue
            if clean_line:
                formatted_summary_lines.append(f"- {clean_line}")

# Append the actual last AI answer at the end
        formatted_summary = "\n".join(formatted_summary_lines + [f"\nAnswer: {answer}"])
    
        summary_message = HumanMessage(
            content=formatted_summary,
            additional_kwargs={"is_summary": True})             
        logger.info("Produced Summary: %s", formatted_summary)
        new_messages = state["messages"] + [summary_message] 
        return {
        "messages": new_messages,
        "curr_file": state["curr_file"],
        "curr_line": state["curr_line"],
        "workdir": state["workdir"],
    }

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

        self.graph_builder.add_node("summarize_messages", self.summarize_messages)
        self.graph_builder.add_node("tool_node", tool_node)
        self.graph_builder.add_node("post_tool_hook", self.post_tool_hook)

        self.graph_builder.add_edge(START, "agent")
        self.graph_builder.add_edge("agent", "tool_node")
        self.graph_builder.add_edge("tool_node", "post_tool_hook")

        self.graph_builder.add_conditional_edges(
        "agent",
        self.check_if_summaries_needed,  # This must return True or False
        {
            True: "summarize_messages",
            False: "tool_node",
        }
    )
        self.graph_builder.add_edge("summarize_messages", "agent")

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
        
    def save_agent_graph_to_png(self):
        with open("./agent_graph.png", "wb") as png:
            png.write(self.graph.get_graph().draw_mermaid_png())

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
