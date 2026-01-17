import json
import logging
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from clients.stratus.stratus_agent.state import State
from clients.stratus.tools.stratus_tool_node import StratusToolNode

logger = logging.getLogger("all.stratus.base")
logger.propagate = True
logger.setLevel(logging.DEBUG)


class BaseAgent:
    def __init__(self, llm, max_step, sync_tools, async_tools, submit_tool, tool_descs):
        self.graph_builder = StateGraph(State)
        self.graph: CompiledStateGraph | None = None
        self.max_step = max_step
        self.async_tools = async_tools
        self.sync_tools = sync_tools
        self.llm = llm
        self.tool_descs = tool_descs
        self.submit_tool = submit_tool
        self.force_submit_prompt_inject_node = "force_submit_thinking_step"
        self.force_submit_tool_call_node = "force_submit_tool_call"
        self.force_submit_tool_execute_node = "force_submit_tool_execute"
        self.llm_force_submit_tool_execute_node = StratusToolNode(sync_tools=[], async_tools=[submit_tool])
        self.thinking_prompt_inject_node = "pre_thinking_step"
        self.thinking_node = "thinking_step"
        self.tool_calling_prompt_inject_node = "pre_tool_calling_step"
        self.tool_calling_node = "tool_calling_step"
        self.process_tool_call_node = "process_tool_call"
        self.post_round_process_node = "post_round_process"
        self.callback = UsageMetadataCallbackHandler()
        self.loop_count = 0

    def llm_inference_step(self, messages, tools):
        return self.llm.inference(messages=messages, tools=tools)

    def llm_thinking_prompt_inject_step(self, state: State):
        # Only include full tool descriptions on the first iteration to save context
        if self.loop_count == 0:
            content = (
                "You are now in the thinking stage. Here are all the tools you can use:\n"
                + self.tool_descs
                + "Choose a tool from the list and output the tool name. Justify your tool choice. In the next step, you will generate a tool call for this tool"
            )
            self.logger.debug(f"[Loop {self.loop_count}] Inject framework prompt: \n {content}")
        else:
            content = (
                "You are now in the thinking stage. Choose a tool from the available tools and justify your choice."
            )
            self.logger.debug(f"[Loop {self.loop_count}] Inject short thinking prompt to save context")

        human_prompt = HumanMessage(content=content)
        return {
            "messages": [human_prompt],
        }

    def llm_thinking_step(self, state: State):
        # planning step, not providing tool
        ai_message = self.llm_inference_step(state["messages"], tools=None)
        self.logger.debug(
            f"[Loop {self.loop_count}] Ask, and LLM responds: \n {ai_message.content}",
            extra={"Full Prompt": state["messages"]},
        )
        if ai_message.content == "Server side error":
            return {
                "messages": [],
            }
        return {
            "messages": [ai_message],
        }

    def llm_tool_call_prompt_inject_step(self, state: State):
        human_prompt = HumanMessage(content="Now generate a tool call according to your last chosen tool.")
        if self.loop_count == 0:
            self.logger.debug(f"[Loop {self.loop_count}] Inject tool call prompt: \n {human_prompt.content}")
        else:
            self.logger.debug(f"[Loop {self.loop_count}] Inject tool call prompt (repeated)")
        return {
            "messages": [human_prompt],
        }

    def llm_tool_call_step(self, state: State):
        if self.sync_tools is None:
            if self.async_tools is not None:
                ai_message = self.llm_inference_step(state["messages"], tools=self.async_tools)
            else:
                raise ValueError("the agent must have at least 1 tool!")
        else:
            if self.async_tools is None:
                ai_message = self.llm_inference_step(state["messages"], tools=self.sync_tools)
            else:
                ai_message = self.llm_inference_step(state["messages"], tools=[*self.sync_tools, *self.async_tools])

        self.logger.debug(
            f"[Loop {self.loop_count}] Tool call",
            extra={"Full Prompt": state["messages"]},
        )
        if ai_message.content == "Server side error":
            return {
                "messages": [],
            }
        return {
            "messages": [ai_message],
        }

    def should_submit_router(self, state: State):
        should_submit = state["num_steps"] == self.max_step and state["submitted"] == False
        self.logger.info(f"Should we force the agent submit? {'Yes!' if should_submit else 'No!'}")
        return self.force_submit_prompt_inject_node if should_submit else self.post_round_process_node

    def post_round_process(self, state: State):
        self.logger.debug("agent finished a round")
        self.logger.debug("currently only incrementing step")
        self.logger.info(f"{'^' * 20} [Loop {self.loop_count}] {'^' * 20}")
        return {
            "num_steps": state["num_steps"] + 1,
        }

    def llm_force_submit_thinking_step(self, state: State):
        human_prompt = HumanMessage(
            content="You have reached your step limit, please submit your results by generating a `submit` tool's tool call."
        )
        self.logger.warning("Agent has not solved the problem until the step limit, force submission.")
        return {"messages": [human_prompt]}

    def llm_force_submit_tool_call_step(self, state: State):
        result = self.llm_inference_step(state["messages"], tools=[self.submit_tool])
        # self.logger.info(f"[Loop {self.loop_count}] Force submit, and LLM responds: \n {result.content}")
        return {"messages": result}

    def clear_memory(self):
        if not hasattr(self, "memory_saver"):
            raise RuntimeError("Should not be called on uninitialized agent. Did you call build_agent()?")
        # source: https://github.com/langchain-ai/langchain/discussions/19744#discussioncomment-13734390
        thread_id = "1"
        try:
            if hasattr(self.memory_saver, "storage") and hasattr(self.memory_saver, "writes"):
                self.memory_saver.storage.pop(thread_id, None)

                keys_to_remove = [key for key in self.memory_saver.writes.keys() if key[0] == thread_id]
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
        # Add tool calls if present (for AIMessage)
        if hasattr(message, "tool_calls") and message.tool_calls:
            msg_dict["tool_calls"] = message.tool_calls
        # Add additional kwargs if present
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
        if output_dir is None:
            output_dir = Path(".")
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trajectory_file = output_dir / f"{agent_name}_trajectory_{timestamp}.jsonl"

        with open(trajectory_file, "w", encoding="utf-8") as f:
            # Write metadata
            metadata = {
                "type": "metadata",
                "agent_name": agent_name,
                "timestamp": timestamp,
                "timestamp_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_events": len(graph_events),
            }
            f.write(json.dumps(metadata) + "\n")

            # Write each graph event
            for idx, event in enumerate(graph_events):
                event_data = {
                    "type": "event",
                    "event_index": idx,
                    "num_steps": event.get("num_steps", 0),
                    "submitted": event.get("submitted", False),
                    "rollback_stack": event.get("rollback_stack", ""),
                }

                # Serialize messages
                if "messages" in event and event["messages"]:
                    event_data["messages"] = [self._serialize_message(msg) for msg in event["messages"]]
                    # Also include just the last message for easier inspection
                    event_data["last_message"] = self._serialize_message(event["messages"][-1])

                f.write(json.dumps(event_data) + "\n")

        logger.info(f"Saved trajectory to {trajectory_file}")
        return trajectory_file

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
            "rollback_stack": "",
        }

        return list(
            self.graph.stream(
                state,
                # recursion_limit could be as large as possible as we have our own limit.
                config={"recursion_limit": 10000, "configurable": {"thread_id": "1"}, "callbacks": [self.callback]},
                stream_mode="values",
            )
        )[-1]

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
            logger.info(f"{'-' * 20} [Loop {self.loop_count}] {'-' * 20}")
            last_state = self.graph.get_state(config=graph_config)
            if len(last_state.values) != 0:
                logger.debug(f"[Loop {self.loop_count}] There were last {len(last_state.values)} states.")
                # this is all the previous msgs the agent had, we just inherit them in the next graph traversal
                state = last_state.values
            else:
                logger.debug(f"[Loop {self.loop_count}] There were no states.")
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
                    "rollback_stack": "",
                }

            async for event in self.graph.astream(
                state,
                # recursion_limit could be as large as possible as we have our own limit.
                config={"recursion_limit": 10000, "configurable": {"thread_id": "1"}, "callbacks": [self.callback]},
                stream_mode="values",
            ):
                if (not graph_events) or event["messages"] != graph_events[-1]["messages"]:
                    event["messages"][-1].pretty_print()
                graph_events.append(event)
            last_state = self.graph.get_state(config=graph_config)
            if last_state.values["submitted"]:
                logger.info(f"[Loop {self.loop_count}] Agent submitted, breaking loop from base_agent")
                # Ensure the final state is included in graph_events
                if not graph_events or last_state.values["messages"] != graph_events[-1]["messages"]:
                    graph_events.append(last_state.values)
                break

            self.loop_count += 1

        return last_state, graph_events
