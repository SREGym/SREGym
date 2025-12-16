"""
MCP Tool Call Interceptor

This module provides a wrapper to intercept and trace all MCP tool calls
for comprehensive meta-agent trace collection.
"""

import functools
import logging
import time
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


class MCPToolCallInterceptor:
    """Interceptor for capturing MCP tool calls"""

    def __init__(self):
        self.meta_agent = None
        self.current_trace_id = None
        self.trace_id_getter = None
        self.enabled = False

    def configure(self, meta_agent, trace_id: str = None, trace_id_getter=None):
        """Configure the interceptor with meta-agent and trace ID"""
        self.meta_agent = meta_agent
        if trace_id_getter:
            self.trace_id_getter = trace_id_getter
            self.current_trace_id = None
        else:
            self.current_trace_id = trace_id
            self.trace_id_getter = None
        self.enabled = True
        logger.info(f"🔧 MCP Tool Call Interceptor configured for trace: {trace_id}")

    def get_current_trace_id(self):
        """Get the current trace ID, using getter function if provided"""
        if self.trace_id_getter:
            return self.trace_id_getter()
        return self.current_trace_id

    def disable(self):
        """Disable the interceptor"""
        self.enabled = False
        self.current_trace_id = None
        self.trace_id_getter = None
        logger.info("🔧 MCP Tool Call Interceptor disabled")

    def trace_tool_call(self, tool_name: str, arguments: Dict[str, Any], response: str, success: bool, duration: float):
        """Trace a tool call to the meta-agent"""
        if not self.enabled or not self.meta_agent:
            return

        # Get current trace ID (using getter if available)
        trace_id = self.get_current_trace_id()
        if not trace_id:
            return

        try:
            # Add tool call to trace
            self.meta_agent.add_tool_call(
                trace_id, tool_name, arguments, success, str(response) if response else "", duration
            )

            # Add thinking step
            thinking_reasoning = f"Used MCP tool {tool_name} with args: {arguments}"
            if success:
                thinking_reasoning += f" - Success: {str(response)[:200]}"
            else:
                thinking_reasoning += f" - Failed: {str(response)[:200]}"

            self.meta_agent.add_thinking_step(
                trace_id, thinking_reasoning, tool_name, f"MCP tool call, duration: {duration:.2f}s"
            )

            logger.info(f"🔧 Traced MCP tool call: {tool_name} ({'success' if success else 'failed'})")

        except Exception as e:
            logger.error(f"Error tracing MCP tool call: {e}")


# Global interceptor instance
global_interceptor = MCPToolCallInterceptor()


def intercept_mcp_tool(func: Callable) -> Callable:
    """Decorator to intercept MCP tool calls"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not global_interceptor.enabled:
            return func(*args, **kwargs)

        tool_name = func.__name__
        start_time = time.time()

        try:
            # Call the original function
            result = func(*args, **kwargs)
            duration = time.time() - start_time

            # Trace successful call
            global_interceptor.trace_tool_call(tool_name, kwargs if kwargs else {}, str(result), True, duration)

            return result

        except Exception as e:
            duration = time.time() - start_time

            # Trace failed call
            global_interceptor.trace_tool_call(
                tool_name,
                kwargs if kwargs else {},
                str(e),
                False,
                duration,
            )

            # Re-raise the exception
            raise

    return wrapper


def wrap_mcp_tool(tool_name: str, original_func: Callable) -> Callable:
    """Wrap an MCP tool function with interception"""

    @functools.wraps(original_func)
    def wrapper(*args, **kwargs):
        if not global_interceptor.enabled:
            return original_func(*args, **kwargs)

        start_time = time.time()

        try:
            # Call the original function
            result = original_func(*args, **kwargs)
            duration = time.time() - start_time

            # Extract arguments
            arguments = kwargs if kwargs else {}
            if args:
                arguments["args"] = args

            # Trace successful call
            global_interceptor.trace_tool_call(tool_name, arguments, str(result), True, duration)

            return result

        except Exception as e:
            duration = time.time() - start_time

            # Extract arguments
            arguments = kwargs if kwargs else {}
            if args:
                arguments["args"] = args

            # Trace failed call
            global_interceptor.trace_tool_call(
                tool_name,
                arguments,
                str(e),
                False,
                duration,
            )

            # Re-raise the exception
            raise

    return wrapper


def enable_interception(meta_agent, trace_id: str = None, trace_id_getter=None):
    """Enable MCP tool call interception

    Args:
        meta_agent: Meta agent instance
        trace_id: Static trace ID (if provided)
        trace_id_getter: Function that returns the current trace ID (takes precedence over trace_id)
    """
    global_interceptor.configure(meta_agent, trace_id, trace_id_getter)


def disable_interception():
    """Disable MCP tool call interception"""
    global_interceptor.disable()




