import json
import logging
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from clients.stratus.stratus_agent.state import State
from clients.stratus.tools.stratus_tool_node import StratusToolNode

logger = logging.getLogger("all.stratus.base")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class BaseAgent:
    def __init__(self, llm, max_step, sync_tools, async_tools, submit_tool):
        self.graph_builder = StateGraph(State)
        self.graph: CompiledStateGraph | None = None
        self.max_step = max_step
        self.async_tools = async_tools
        self.sync_tools = sync_tools
        self.llm = llm
        self.submit_tool = submit_tool
        self.callback = UsageMetadataCallbackHandler()
        self.logger = logging.getLogger("all.stratus.base")

    def _all_tools(self):
        tools = []
        if self.sync_tools:
            tools.extend(self.sync_tools)
        if self.async_tools:
            tools.extend(self.async_tools)
        if not tools:
            raise ValueError("Agent must have at least one tool!")
        return tools

    def call_model(self, state: State):
        ai_message = self.llm.inference(messages=state["messages"], tools=self._all_tools())
        self.logger.debug(f"[Step {state['num_steps']}] LLM response: {ai_message.content}")
        if ai_message.content == "Server side error":
            return {"messages": []}
        return {"messages": [ai_message]}

    def should_continue(self, state: State):
        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return END
        return "tool_node"

    def after_tools(self, state: State):
        if state["submitted"]:
            return END
        if state["num_steps"] >= self.max_step:
            return "force_submit"
        return "call_model"

    def force_submit(self, state: State):
        self.logger.warning(f"Agent reached step limit ({self.max_step}), forcing submission.")
        prompt = HumanMessage("You have reached your step limit. Please submit your results using the submit tool.")
        ai_message = self.llm.inference(messages=state["messages"] + [prompt], tools=[self.submit_tool])
        return {"messages": [prompt, ai_message]}

    def post_round_process(self, state: State):
        self.logger.info(f"{'~' * 20} [Step {state['num_steps']}] {'~' * 20}")
        filtered_messages = self._filter_rejected_command_errors(state["messages"])
        return {"num_steps": state["num_steps"] + 1, "messages": filtered_messages}

    def _filter_rejected_command_errors(self, messages: list) -> list:
        """
        Remove 'Command Rejected' error messages from context if a successful command was executed.

        This prevents wasted context window from keeping rejection error messages after
        Stratus successfully generates a correct command.
        """
        if len(messages) < 2:
            return messages

        last_message = messages[-1]
        if not isinstance(last_message, ToolMessage):
            return messages

        if "Command Rejected" in last_message.content:
            return messages

        filtered_messages = []
        removed_count = 0

        for i, msg in enumerate(messages):
            if i == len(messages) - 1:
                filtered_messages.append(msg)
                continue

            if isinstance(msg, ToolMessage) and "Command Rejected" in msg.content:
                removed_count += 1
                self.logger.debug(f"Removing rejected command error message: {msg.content[:100]}...")
                continue

            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                if i + 1 < len(messages) and isinstance(messages[i + 1], ToolMessage):
                    if "Command Rejected" in messages[i + 1].content:
                        removed_count += 1
                        self.logger.debug("Removing AIMessage with rejected tool call")
                        continue

            filtered_messages.append(msg)

        if removed_count > 0:
            self.logger.info(
                f"Filtered {removed_count} rejected command error messages from context "
                f"(reduced from {len(messages)} to {len(filtered_messages)} messages)"
            )

        return filtered_messages

    def build_agent(self):
        tool_node = StratusToolNode(sync_tools=self.sync_tools or [], async_tools=self.async_tools or [])
        force_submit_execute = StratusToolNode(sync_tools=[], async_tools=[self.submit_tool])

        self.graph_builder.add_node("call_model", self.call_model)
        self.graph_builder.add_node("tool_node", tool_node)
        self.graph_builder.add_node("post_round_process", self.post_round_process)
        self.graph_builder.add_node("force_submit", self.force_submit)
        self.graph_builder.add_node("force_submit_execute", force_submit_execute)

        self.graph_builder.add_edge(START, "call_model")
        self.graph_builder.add_conditional_edges("call_model", self.should_continue)
        self.graph_builder.add_edge("tool_node", "post_round_process")
        self.graph_builder.add_conditional_edges("post_round_process", self.after_tools)
        self.graph_builder.add_edge("force_submit", "force_submit_execute")
        self.graph_builder.add_edge("force_submit_execute", END)

        self.memory_saver = MemorySaver()
        self.graph = self.graph_builder.compile(checkpointer=self.memory_saver)

    def clear_memory(self):
        if not hasattr(self, "memory_saver"):
            raise RuntimeError("Should not be called on uninitialized agent. Did you call build_agent()?")
        # source: https://github.com/langchain-ai/langchain/discussions/19744#discussioncomment-13734390
        thread_id = "1"
        try:
            if hasattr(self.memory_saver, "storage") and hasattr(self.memory_saver, "writes"):
                self.memory_saver.storage.pop(thread_id, None)

                keys_to_remove = [key for key in self.memory_saver.writes if key[0] == thread_id]
                for key in keys_to_remove:
                    self.memory_saver.writes.pop(key, None)

                print(f"Memory cleared for thread_id: {thread_id}")
                return
        except Exception as e:
            logger.error(f"Error clearing InMemorySaver storage for thread_id {thread_id}: {e}")

    def _serialize_message(self, message):
        """Convert a LangChain message to a serializable dict"""
        msg_dict = {
            "type": message.__class__.__name__,
            "content": message.content,
        }
        if hasattr(message, "tool_calls") and message.tool_calls:
            msg_dict["tool_calls"] = message.tool_calls
        if hasattr(message, "additional_kwargs") and message.additional_kwargs:
            msg_dict["additional_kwargs"] = message.additional_kwargs
        return msg_dict

    def save_trajectory(self, graph_events, agent_name, output_dir=None):
        """
        Save agent trajectory to JSONL file.

        Args:
            graph_events: List of graph state events from astream
            agent_name: Name of the agent (e.g., "diagnosis", "mitigation")
            output_dir: Directory to save trajectory (defaults to current directory)
        """
        output_dir = Path(".") if output_dir is None else Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trajectory_file = output_dir / f"{agent_name}_trajectory_{timestamp}.jsonl"

        with open(trajectory_file, "w", encoding="utf-8") as f:
            metadata = {
                "type": "metadata",
                "agent_name": agent_name,
                "timestamp": timestamp,
                "timestamp_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_events": len(graph_events),
            }
            f.write(json.dumps(metadata) + "\n")

            for idx, event in enumerate(graph_events):
                event_data = {
                    "type": "event",
                    "event_index": idx,
                    "num_steps": event.get("num_steps", 0),
                    "submitted": event.get("submitted", False),
                    "rollback_stack": event.get("rollback_stack", ""),
                }

                if "messages" in event and event["messages"]:
                    event_data["messages"] = [self._serialize_message(msg) for msg in event["messages"]]
                    event_data["last_message"] = self._serialize_message(event["messages"][-1])

                f.write(json.dumps(event_data) + "\n")

        logger.info(f"Saved trajectory to {trajectory_file}")
        return trajectory_file

    async def arun(self, starting_prompts):
        """
        Run the agent asynchronously.

        Args:
            starting_prompts (list[SystemMessage | HumanMessage]): Initial conversation prompts.

        Returns:
            (last_state, graph_events): Final StateSnapshot and list of all graph state events.
        """
        if not self.graph:
            raise ValueError("Agent graph is None. Have you built the agent?")
        if not starting_prompts:
            raise ValueError("No prompts used to start the conversation!")

        state = {
            "messages": starting_prompts,
            "num_steps": 0,
            "submitted": False,
            "rollback_stack": "",
        }
        graph_config = {
            "recursion_limit": 10000,
            "configurable": {"thread_id": "1"},
            "callbacks": [self.callback],
        }

        graph_events = []
        async for event in self.graph.astream(state, config=graph_config, stream_mode="values"):
            if not graph_events or event["messages"][-1] != graph_events[-1]["messages"][-1]:
                event["messages"][-1].pretty_print()
            graph_events.append(event)

        last_state = self.graph.get_state(config={"configurable": {"thread_id": "1"}})
        return last_state, graph_events
