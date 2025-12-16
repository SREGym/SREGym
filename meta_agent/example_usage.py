"""
Example usage of the Meta-Agent system

This script demonstrates how to use the meta-agent system to:
1. Collect traces from agent executions
2. Analyze patterns and learn from them
3. Update agent guidelines iteratively
4. Monitor the learning process
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

from meta_agent import MetaAgent, MetaAgentConfig
from meta_agent.integration import initialize_meta_agent, trace_agent_execution, trace_tool_call
from meta_agent.trace_collector import AgentType, ProblemContext

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def example_manual_trace_collection():
    """Example of manually collecting traces"""
    print("=== Manual Trace Collection Example ===")

    # Initialize meta-agent
    meta_agent = MetaAgent()

    # Create a problem context
    problem_context = ProblemContext(
        problem_id="example_problem_1",
        app_name="hotel-reservation",
        app_namespace="hotel-reservation",
        app_description="A microservices-based hotel reservation system",
    )

    # Start a trace
    trace_id = "trace_001"
    trace = meta_agent.collect_agent_trace(
        trace_id=trace_id, agent_type=AgentType.DIAGNOSIS, problem_context=problem_context
    )

    # Simulate some tool calls
    meta_agent.add_tool_call(
        trace_id=trace_id,
        tool_name="get_pods",
        arguments={"namespace": "hotel-reservation"},
        success=True,
        response="Found 5 pods running",
        duration=1.2,
    )

    meta_agent.add_thinking_step(
        trace_id=trace_id,
        reasoning="I need to check the pod status to understand the current state",
        tool_choice="get_pods",
        justification="This will give me visibility into the running services",
    )

    meta_agent.add_tool_call(
        trace_id=trace_id,
        tool_name="get_logs",
        arguments={"pod": "user-service-123", "lines": 100},
        success=True,
        response="Error logs found: Connection timeout to database",
        duration=0.8,
    )

    # End the trace
    final_trace = meta_agent.end_agent_trace(
        trace_id=trace_id, success=True, final_submission="Database connection issue identified in user-service"
    )

    print(f"Trace completed: {final_trace.trace_id}")
    print(f"Success: {final_trace.success}")
    print(f"Tool calls: {len(final_trace.tool_calls)}")
    print(f"Duration: {final_trace.performance_metrics.get('total_duration', 0):.2f}s")


def example_learning_cycle():
    """Example of running a learning cycle"""
    print("\n=== Learning Cycle Example ===")

    # Initialize meta-agent
    meta_agent = MetaAgent()

    # Generate some example traces
    generate_example_traces(meta_agent)

    # Run learning cycle
    result = meta_agent.start_learning_cycle()
    print(f"Learning cycle result: {json.dumps(result, indent=2)}")

    # Get learning status
    status = meta_agent.get_learning_status()
    print(f"Learning status: {json.dumps(status, indent=2)}")

    # Get pattern summary
    patterns = meta_agent.get_pattern_summary()
    print(f"Pattern summary: {json.dumps(patterns, indent=2)}")


def generate_example_traces(meta_agent: MetaAgent, num_traces: int = 15):
    """Generate example traces for demonstration"""
    print(f"Generating {num_traces} example traces...")

    for i in range(num_traces):
        # Create problem context
        problem_context = ProblemContext(
            problem_id=f"example_problem_{i}",
            app_name="hotel-reservation",
            app_namespace="hotel-reservation",
            app_description="A microservices-based hotel reservation system",
        )

        # Start trace
        trace_id = f"example_trace_{i}"
        meta_agent.collect_agent_trace(
            trace_id=trace_id, agent_type=AgentType.DIAGNOSIS, problem_context=problem_context
        )

        # Simulate tool calls with some patterns
        tools = ["get_pods", "get_logs", "get_metrics", "check_services"]

        # Simulate successful pattern: get_pods -> get_logs -> check_services
        if i % 3 == 0:  # Every 3rd trace follows successful pattern
            meta_agent.add_tool_call(trace_id, "get_pods", {"namespace": "hotel-reservation"}, True, "Pods found", 1.0)
            meta_agent.add_tool_call(trace_id, "get_logs", {"pod": "user-service"}, True, "Error found", 0.8)
            meta_agent.add_tool_call(trace_id, "check_services", {"service": "user-service"}, True, "Service down", 0.5)
            success = True
        else:
            # Simulate some failures
            meta_agent.add_tool_call(trace_id, "get_pods", {"namespace": "hotel-reservation"}, True, "Pods found", 1.0)
            meta_agent.add_tool_call(
                trace_id, "get_metrics", {"service": "user-service"}, False, "Metrics unavailable", 2.0
            )
            success = i % 2 == 0  # 50% success rate for non-pattern traces

        # End trace
        meta_agent.end_agent_trace(
            trace_id=trace_id, success=success, final_submission=f"Problem analysis completed for trace {i}"
        )


def example_integration_decorators():
    """Example of using integration decorators"""
    print("\n=== Integration Decorators Example ===")

    # Initialize meta-agent manager
    manager = initialize_meta_agent()

    # Example agent function with tracing
    @trace_agent_execution(AgentType.DIAGNOSIS)
    def diagnose_problem(problem_id: str, app_name: str) -> Dict[str, Any]:
        """Example diagnosis function"""

        @trace_tool_call("example_trace")
        def get_pod_status(namespace: str) -> str:
            time.sleep(0.1)  # Simulate work
            return "Pods are running"

        @trace_tool_call("example_trace")
        def check_logs(pod_name: str) -> str:
            time.sleep(0.1)  # Simulate work
            return "No errors found"

        # Simulate diagnosis process
        pod_status = get_pod_status("hotel-reservation")
        logs = check_logs("user-service-123")

        return {
            "problem_id": problem_id,
            "app_name": app_name,
            "status": "healthy",
            "details": {"pods": pod_status, "logs": logs},
        }

    # Run the diagnosis
    result = diagnose_problem("test_problem", "hotel-reservation")
    print(f"Diagnosis result: {result}")

    # Check learning status
    status = manager.get_status()
    print(f"Learning status: {json.dumps(status, indent=2)}")


def example_continuous_learning():
    """Example of continuous learning mode"""
    print("\n=== Continuous Learning Example ===")

    # Create a custom config for faster learning cycles
    config = MetaAgentConfig(
        learning_interval=30,  # 30 seconds between cycles
        min_traces_for_analysis=5,  # Lower threshold for demo
        enable_auto_updates=True,
    )

    meta_agent = MetaAgent(config)

    print("Starting continuous learning (will run for 2 minutes)...")
    print("Press Ctrl+C to stop early")

    try:
        # Run continuous learning for 2 minutes
        start_time = time.time()
        while time.time() - start_time < 120:  # 2 minutes
            if meta_agent.should_start_learning_cycle():
                result = meta_agent.start_learning_cycle()
                print(f"Learning cycle completed: {result}")

            # Generate some traces
            generate_example_traces(meta_agent, 2)
            time.sleep(10)  # Wait 10 seconds between trace generation

        print("Continuous learning completed")

    except KeyboardInterrupt:
        print("Continuous learning stopped by user")

    # Show final status
    status = meta_agent.get_learning_status()
    print(f"Final learning status: {json.dumps(status, indent=2)}")


def example_guideline_management():
    """Example of managing guidelines and versions"""
    print("\n=== Guideline Management Example ===")

    meta_agent = MetaAgent()

    # Generate some traces and run learning
    generate_example_traces(meta_agent, 10)
    meta_agent.start_learning_cycle()

    # Get guideline history
    history = meta_agent.get_guideline_history()
    print(f"Guideline history: {json.dumps(history, indent=2)}")

    # Example of rolling back to a previous version
    if history:
        latest_update = history[0]
        agent_type = AgentType(latest_update["agent_type"])
        version = latest_update["version"]

        print(f"Rolling back {agent_type.value} to version {version}")
        success = meta_agent.rollback_agent(agent_type, version)
        print(f"Rollback successful: {success}")


def main():
    """Run all examples"""
    print("Meta-Agent System Examples")
    print("=" * 50)

    try:
        # Run examples
        example_manual_trace_collection()
        example_learning_cycle()
        example_integration_decorators()
        example_continuous_learning()
        example_guideline_management()

        print("\nAll examples completed successfully!")

    except Exception as e:
        logger.error(f"Example failed: {e}")
        raise


if __name__ == "__main__":
    main()
