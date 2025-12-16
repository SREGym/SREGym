"""
Example integration with existing Stratus agents

This example shows how to integrate the meta-agent system with the existing
Stratus agent infrastructure without major modifications.
"""

import logging

# Import existing Stratus components
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.append("/home/xinbowu2/project/xinbowu2/SREArena")

from clients.stratus.stratus_agent.base_agent import BaseAgent
from clients.stratus.stratus_agent.state import State
from meta_agent.integration import get_integration, trace_agent_execution, trace_tool_call
from meta_agent.trace_collector import AgentType, ProblemContext

logger = logging.getLogger(__name__)


class EnhancedBaseAgent(BaseAgent):
    """Enhanced BaseAgent with meta-agent integration"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_trace_id = None
        self.integration = get_integration()

    def start_trace(self, problem_context: ProblemContext, agent_type: AgentType) -> str:
        """Start tracing this agent execution"""
        self.current_trace_id = f"{agent_type.value}_{int(time.time() * 1000)}"
        self.integration.start_agent_trace(self.current_trace_id, agent_type, problem_context)
        return self.current_trace_id

    def end_trace(self, success: bool, final_submission: Optional[str] = None) -> None:
        """End tracing this agent execution"""
        if self.current_trace_id:
            self.integration.end_agent_trace(self.current_trace_id, success, final_submission)
            self.current_trace_id = None

    def llm_inference_step(self, messages, tools):
        """Enhanced LLM inference with trace recording"""
        start_time = time.time()

        try:
            result = super().llm_inference_step(messages, tools)
            success = True
            response = str(result) if result else ""
        except Exception as e:
            success = False
            response = str(e)
            raise
        finally:
            duration = time.time() - start_time

            if self.current_trace_id:
                self.integration.trace_tool_call(
                    self.current_trace_id,
                    "llm_inference",
                    {"messages_count": len(messages), "tools_count": len(tools)},
                    success,
                    response,
                    duration,
                )

        return result


class EnhancedDiagnosisAgent(EnhancedBaseAgent):
    """Enhanced Diagnosis Agent with meta-agent integration"""

    def run(self, state: State) -> State:
        """Enhanced run method with tracing"""
        # Extract problem context from state
        problem_context = self._extract_problem_context(state)

        # Start tracing
        trace_id = self.start_trace(problem_context, AgentType.DIAGNOSIS)

        try:
            # Run the original diagnosis logic
            result_state = self._run_diagnosis(state)

            # Determine success based on result
            success = self._determine_success(result_state)

            # End tracing
            self.end_trace(success, result_state.get("final_submission"))

            return result_state

        except Exception as e:
            # End tracing with failure
            self.end_trace(False, str(e))
            raise

    def _extract_problem_context(self, state: State) -> ProblemContext:
        """Extract problem context from agent state"""
        # This would need to be adapted based on actual state structure
        return ProblemContext(
            problem_id=getattr(state, "problem_id", "unknown"),
            app_name=getattr(state, "app_name", "unknown"),
            app_namespace=getattr(state, "app_namespace", "unknown"),
            app_description=getattr(state, "app_description", "unknown"),
        )

    def _run_diagnosis(self, state: State) -> State:
        """Original diagnosis logic (placeholder)"""
        # This would contain the actual diagnosis agent logic
        # For now, just return the state with some modifications
        state.final_submission = "Diagnosis completed"
        return state

    def _determine_success(self, state: State) -> bool:
        """Determine if the diagnosis was successful"""
        # This would contain logic to determine success
        # For now, assume success if we have a final submission
        return hasattr(state, "final_submission") and state.final_submission is not None


def create_enhanced_agent(
    agent_type: str, llm, max_step: int, sync_tools: list, async_tools: list, submit_tool, tool_descs: dict
) -> EnhancedBaseAgent:
    """Factory function to create enhanced agents"""

    agent_type_map = {
        "diagnosis": AgentType.DIAGNOSIS,
        "localization": AgentType.LOCALIZATION,
        "mitigation": AgentType.MITIGATION,
        "rollback": AgentType.ROLLBACK,
    }

    if agent_type not in agent_type_map:
        raise ValueError(f"Unknown agent type: {agent_type}")

    # Create enhanced agent
    agent = EnhancedBaseAgent(
        llm=llm,
        max_step=max_step,
        sync_tools=sync_tools,
        async_tools=async_tools,
        submit_tool=submit_tool,
        tool_descs=tool_descs,
    )

    return agent


def example_usage():
    """Example of using the enhanced agents"""
    print("=== Enhanced Stratus Agent Integration Example ===")

    # Initialize meta-agent integration
    integration = get_integration()

    # Create a mock problem context
    problem_context = ProblemContext(
        problem_id="test_problem_001",
        app_name="hotel-reservation",
        app_namespace="hotel-reservation",
        app_description="A microservices-based hotel reservation system",
    )

    # Create enhanced diagnosis agent (with mock components)
    class MockLLM:
        def inference(self, messages, tools):
            return {"response": "Mock LLM response"}

    class MockState:
        def __init__(self):
            self.problem_id = "test_problem_001"
            self.app_name = "hotel-reservation"
            self.app_namespace = "hotel-reservation"
            self.app_description = "A microservices-based hotel reservation system"
            self.final_submission = None

    # Create enhanced agent
    agent = create_enhanced_agent(
        agent_type="diagnosis",
        llm=MockLLM(),
        max_step=10,
        sync_tools=[],
        async_tools=[],
        submit_tool=None,
        tool_descs={},
    )

    # Run the agent
    state = MockState()
    result_state = agent.run(state)

    print(f"Agent execution completed")
    print(f"Final submission: {result_state.final_submission}")

    # Check learning status
    if integration.meta_agent:
        status = integration.meta_agent.get_learning_status()
        print(f"Learning status: {status}")

        # Start a learning cycle if we have enough traces
        if status.get("ready_for_learning", False):
            result = integration.meta_agent.start_learning_cycle()
            print(f"Learning cycle result: {result}")


if __name__ == "__main__":
    example_usage()
