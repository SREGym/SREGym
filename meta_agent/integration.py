"""
Integration module for Meta-Agent with existing Stratus agents

Provides hooks and decorators to integrate the meta-agent system
with the existing Stratus agent infrastructure.
"""

import functools
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .meta_agent import MetaAgent, MetaAgentConfig
from .trace_collector import AgentType, ProblemContext

logger = logging.getLogger(__name__)


class StratusMetaAgentIntegration:
    """Integration layer between Stratus agents and Meta-Agent system"""

    def __init__(self, meta_agent: Optional[MetaAgent] = None):
        self.meta_agent = meta_agent or MetaAgent()
        self.active_traces: Dict[str, str] = {}  # trace_id -> agent_type mapping

    def start_agent_trace(self, trace_id: str, agent_type: AgentType, problem_context: ProblemContext) -> None:
        """Start tracing an agent execution"""
        try:
            self.meta_agent.collect_agent_trace(trace_id, agent_type, problem_context)
            self.active_traces[trace_id] = agent_type.value
            logger.info(f"Started trace {trace_id} for {agent_type.value} agent")
        except Exception as e:
            logger.error(f"Failed to start trace {trace_id}: {e}")

    def end_agent_trace(self, trace_id: str, success: bool, final_submission: Optional[str] = None) -> None:
        """End tracing an agent execution"""
        try:
            if trace_id in self.active_traces:
                self.meta_agent.end_agent_trace(trace_id, success, final_submission)
                del self.active_traces[trace_id]
                logger.info(f"Ended trace {trace_id} - Success: {success}")
            else:
                logger.warning(f"Trace {trace_id} not found in active traces")
        except Exception as e:
            logger.error(f"Failed to end trace {trace_id}: {e}")

    def trace_tool_call(
        self, trace_id: str, tool_name: str, arguments: Dict[str, Any], success: bool, response: str, duration: float
    ) -> None:
        """Record a tool call in the current trace"""
        try:
            if trace_id in self.active_traces:
                self.meta_agent.add_tool_call(trace_id, tool_name, arguments, success, response, duration)
        except Exception as e:
            logger.error(f"Failed to record tool call for trace {trace_id}: {e}")

    def trace_thinking_step(self, trace_id: str, reasoning: str, tool_choice: str, justification: str) -> None:
        """Record a thinking step in the current trace"""
        try:
            if trace_id in self.active_traces:
                self.meta_agent.add_thinking_step(trace_id, reasoning, tool_choice, justification)
        except Exception as e:
            logger.error(f"Failed to record thinking step for trace {trace_id}: {e}")


# Global integration instance
_integration = None


def get_integration() -> StratusMetaAgentIntegration:
    """Get the global integration instance"""
    global _integration
    if _integration is None:
        _integration = StratusMetaAgentIntegration()
    return _integration


def trace_agent_execution(agent_type: AgentType):
    """Decorator to automatically trace agent execution"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Generate trace ID
            trace_id = f"{agent_type.value}_{int(time.time() * 1000)}"

            # Extract problem context from arguments if possible
            problem_context = None
            if "problem" in kwargs:
                problem = kwargs["problem"]
                problem_context = ProblemContext(
                    problem_id=getattr(problem, "id", "unknown"),
                    app_name=getattr(problem, "app_name", "unknown"),
                    app_namespace=getattr(problem, "app_namespace", "unknown"),
                    app_description=getattr(problem, "app_description", "unknown"),
                )
            elif len(args) > 0 and hasattr(args[0], "problem"):
                problem = args[0].problem
                problem_context = ProblemContext(
                    problem_id=getattr(problem, "id", "unknown"),
                    app_name=getattr(problem, "app_name", "unknown"),
                    app_namespace=getattr(problem, "app_namespace", "unknown"),
                    app_description=getattr(problem, "app_description", "unknown"),
                )

            if problem_context is None:
                # Create default context
                problem_context = ProblemContext(
                    problem_id="unknown", app_name="unknown", app_namespace="unknown", app_description="unknown"
                )

            # Start tracing
            integration = get_integration()
            integration.start_agent_trace(trace_id, agent_type, problem_context)

            try:
                # Execute the original function
                result = func(*args, **kwargs)

                # Determine success based on result
                success = True
                if hasattr(result, "success"):
                    success = result.success
                elif isinstance(result, bool):
                    success = result
                elif hasattr(result, "status") and "error" in str(result.status).lower():
                    success = False

                # End tracing
                integration.end_agent_trace(trace_id, success)

                return result

            except Exception as e:
                # End tracing with failure
                integration.end_agent_trace(trace_id, False, str(e))
                raise

        return wrapper

    return decorator


def trace_tool_call(trace_id: str):
    """Decorator to trace individual tool calls"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            success = False
            response = ""

            try:
                result = func(*args, **kwargs)
                success = True
                response = str(result) if result is not None else ""
                return result
            except Exception as e:
                response = str(e)
                raise
            finally:
                duration = time.time() - start_time

                # Extract tool name and arguments
                tool_name = func.__name__
                arguments = kwargs.copy()

                # Record the tool call
                integration = get_integration()
                integration.trace_tool_call(trace_id, tool_name, arguments, success, response, duration)

        return wrapper

    return decorator


def trace_thinking_step(trace_id: str):
    """Decorator to trace thinking steps"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Extract reasoning and tool choice from function arguments
            reasoning = kwargs.get("reasoning", "")
            tool_choice = kwargs.get("tool_choice", "")
            justification = kwargs.get("justification", "")

            # Execute the original function
            result = func(*args, **kwargs)

            # Record the thinking step
            integration = get_integration()
            integration.trace_thinking_step(trace_id, reasoning, tool_choice, justification)

            return result

        return wrapper

    return decorator


class MetaAgentManager:
    """Manager class for the meta-agent system"""

    def __init__(self, config_file: Optional[str] = None):
        self.config_file = config_file
        self.meta_agent = None
        self.integration = None

        if config_file and Path(config_file).exists():
            self.load_config(config_file)
        else:
            self.meta_agent = MetaAgent()
            self.integration = StratusMetaAgentIntegration(self.meta_agent)

    def load_config(self, config_file: str) -> None:
        """Load configuration from file"""
        try:
            import json

            with open(config_file, "r") as f:
                config_data = json.load(f)

            config = MetaAgentConfig(**config_data.get("config", {}))
            self.meta_agent = MetaAgent(config)
            self.integration = StratusMetaAgentIntegration(self.meta_agent)

            # Load state if available
            if "state_file" in config_data:
                self.meta_agent.load_state(config_data["state_file"])

            logger.info(f"Loaded meta-agent configuration from {config_file}")

        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            self.meta_agent = MetaAgent()
            self.integration = StratusMetaAgentIntegration(self.meta_agent)

    def start_learning_cycle(self) -> Dict[str, Any]:
        """Start a learning cycle"""
        if self.meta_agent:
            return self.meta_agent.start_learning_cycle()
        return {"status": "error", "message": "Meta-agent not initialized"}

    def get_status(self) -> Dict[str, Any]:
        """Get current status"""
        if self.meta_agent:
            return self.meta_agent.get_learning_status()
        return {"status": "error", "message": "Meta-agent not initialized"}

    def get_patterns(self) -> Dict[str, Any]:
        """Get learned patterns"""
        if self.meta_agent:
            return self.meta_agent.get_pattern_summary()
        return {"status": "error", "message": "Meta-agent not initialized"}

    def get_guideline_history(self, agent_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get guideline update history"""
        if self.meta_agent:
            agent_enum = None
            if agent_type:
                try:
                    agent_enum = AgentType(agent_type)
                except ValueError:
                    logger.warning(f"Invalid agent type: {agent_type}")
            return self.meta_agent.get_guideline_history(agent_enum)
        return []


# Convenience functions for easy integration
def initialize_meta_agent(config_file: Optional[str] = None) -> MetaAgentManager:
    """Initialize the meta-agent system"""
    return MetaAgentManager(config_file)


def start_learning() -> Dict[str, Any]:
    """Start a learning cycle"""
    integration = get_integration()
    if integration.meta_agent:
        return integration.meta_agent.start_learning_cycle()
    return {"status": "error", "message": "Meta-agent not initialized"}


def get_learning_status() -> Dict[str, Any]:
    """Get learning status"""
    integration = get_integration()
    if integration.meta_agent:
        return integration.meta_agent.get_learning_status()
    return {"status": "error", "message": "Meta-agent not initialized"}
