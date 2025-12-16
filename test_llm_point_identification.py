#!/usr/bin/env python3
"""
Test script to compare heuristic-first vs LLM-primary point identification
using existing traces and points from previous runs.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from meta_agent.point_based_prompts import PointBasedPromptManager
from meta_agent.trace_collector import AgentTrace, AgentType, TraceCollector, ToolCall, ThinkingStep, ProblemContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_trace_from_file(trace_file: Path) -> AgentTrace:
    """Load a trace from a JSON file"""
    with open(trace_file, "r") as f:
        data = json.load(f)
    
    # Convert agent_type string to enum
    if isinstance(data.get("agent_type"), str):
        try:
            data["agent_type"] = AgentType(data["agent_type"])
        except ValueError:
            # Fallback
            agent_type_map = {
                "diagnosis": AgentType.DIAGNOSIS,
                "localization": AgentType.LOCALIZATION,
                "mitigation": AgentType.MITIGATION,
                "rollback": AgentType.ROLLBACK,
            }
            data["agent_type"] = agent_type_map.get(data["agent_type"], AgentType.DIAGNOSIS)
    
    # Convert problem_context
    if "problem_context" in data and isinstance(data["problem_context"], dict):
        data["problem_context"] = ProblemContext(**data["problem_context"])
    
    # Convert tool_calls
    if "tool_calls" in data and data["tool_calls"]:
        tool_calls = []
        for tc in data["tool_calls"]:
            if isinstance(tc, dict):
                tool_calls.append(ToolCall(**tc))
            else:
                tool_calls.append(tc)
        data["tool_calls"] = tool_calls
    
    # Convert thinking_steps
    if "thinking_steps" in data and data["thinking_steps"]:
        thinking_steps = []
        for ts in data["thinking_steps"]:
            if isinstance(ts, dict):
                thinking_steps.append(ThinkingStep(**ts))
            else:
                thinking_steps.append(ts)
        data["thinking_steps"] = thinking_steps
    
    return AgentTrace(**data)


def test_point_identification(
    trace: AgentTrace,
    points_storage: Path,
    use_llm_primary: bool = False
) -> List[str]:
    """Test point identification with a given method"""
    # Create point manager with specified mode
    point_manager = PointBasedPromptManager(
        storage_path=str(points_storage),
        use_llm_detection=True,
        use_llm_usage_detection=True,
        skip_load=False,
        use_llm_primary=use_llm_primary
    )
    
    # Identify used points
    used_points = point_manager.identify_used_points(trace.agent_type, trace)
    
    return used_points


def main():
    """Main test function"""
    # Find a recent run with traces and points
    runs_dir = Path("llm_learning_results")
    
    # Try to find a run with both traces and points
    test_run = None
    for run_dir in sorted(runs_dir.glob("run_*"), reverse=True):
        traces_dir = run_dir / "traces"
        points_dir = run_dir / "points"
        
        if traces_dir.exists() and points_dir.exists():
            trace_files = list(traces_dir.glob("*.json"))
            point_files = list(points_dir.glob("*_points.json"))
            
            if trace_files and point_files:
                test_run = run_dir
                logger.info(f"Found test run: {test_run}")
                break
    
    if not test_run:
        logger.error("No suitable test run found with both traces and points")
        return
    
    traces_dir = test_run / "traces"
    points_dir = test_run / "points"
    
    # Load traces with actual activity (tool calls or thinking steps)
    all_trace_files = list(traces_dir.glob("*.json"))
    trace_files = []
    
    for tf in all_trace_files:
        try:
            with open(tf, "r") as f:
                data = json.load(f)
            tool_count = len(data.get("tool_calls", []))
            thinking_count = len(data.get("thinking_steps", []))
            if tool_count > 0 or thinking_count > 0:
                trace_files.append(tf)
                if len(trace_files) >= 5:  # Get up to 5 traces with activity
                    break
        except:
            continue
    
    if not trace_files:
        # Fallback: use any traces if none have activity
        trace_files = all_trace_files[:5]
        logger.warning("No traces with tool calls found, using any available traces")
    
    if not trace_files:
        logger.error(f"No trace files found in {traces_dir}")
        return
    
    logger.info(f"Testing with {len(trace_files)} traces from {test_run}")
    
    results = {
        "heuristic_first": {"total_identified": 0, "per_trace": []},
        "llm_primary": {"total_identified": 0, "per_trace": []}
    }
    
    for trace_file in trace_files:
        try:
            trace = load_trace_from_file(trace_file)
            logger.info(f"\n{'='*60}")
            logger.info(f"Testing trace: {trace_file.name}")
            logger.info(f"Agent Type: {trace.agent_type.value}")
            logger.info(f"Success: {trace.success}")
            logger.info(f"Tool Calls: {len(trace.tool_calls)}")
            logger.info(f"Thinking Steps: {len(trace.thinking_steps)}")
            
            # Test heuristic-first method
            logger.info("\n--- Testing HEURISTIC-FIRST method ---")
            heuristic_points = test_point_identification(
                trace, points_dir, use_llm_primary=False
            )
            logger.info(f"Identified {len(heuristic_points)} points: {heuristic_points[:3]}...")
            results["heuristic_first"]["total_identified"] += len(heuristic_points)
            results["heuristic_first"]["per_trace"].append({
                "trace": trace_file.name,
                "count": len(heuristic_points),
                "points": heuristic_points
            })
            
            # Test LLM-primary method
            logger.info("\n--- Testing LLM-PRIMARY method ---")
            llm_points = test_point_identification(
                trace, points_dir, use_llm_primary=True
            )
            logger.info(f"Identified {len(llm_points)} points: {llm_points[:3]}...")
            results["llm_primary"]["total_identified"] += len(llm_points)
            results["llm_primary"]["per_trace"].append({
                "trace": trace_file.name,
                "count": len(llm_points),
                "points": llm_points
            })
            
            # Compare
            logger.info("\n--- Comparison ---")
            logger.info(f"Heuristic-first: {len(heuristic_points)} points")
            logger.info(f"LLM-primary: {len(llm_points)} points")
            logger.info(f"Improvement: {len(llm_points) - len(heuristic_points)} points")
            
            # Show points only in LLM method
            llm_only = set(llm_points) - set(heuristic_points)
            if llm_only:
                logger.info(f"Points found only by LLM: {llm_only}")
            
        except Exception as e:
            logger.error(f"Error processing {trace_file}: {e}", exc_info=True)
            continue
    
    # Summary
    logger.info(f"\n{'='*60}")
    logger.info("SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total traces tested: {len(trace_files)}")
    logger.info(f"\nHeuristic-First Method:")
    logger.info(f"  Total points identified: {results['heuristic_first']['total_identified']}")
    logger.info(f"  Average per trace: {results['heuristic_first']['total_identified'] / len(trace_files):.2f}")
    
    logger.info(f"\nLLM-Primary Method:")
    logger.info(f"  Total points identified: {results['llm_primary']['total_identified']}")
    logger.info(f"  Average per trace: {results['llm_primary']['total_identified'] / len(trace_files):.2f}")
    
    improvement = results['llm_primary']['total_identified'] - results['heuristic_first']['total_identified']
    improvement_pct = (improvement / max(results['heuristic_first']['total_identified'], 1)) * 100
    logger.info(f"\nImprovement: {improvement} points ({improvement_pct:+.1f}%)")
    
    # Save detailed results
    results_file = Path("test_point_identification_results.json")
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"\nDetailed results saved to: {results_file}")


if __name__ == "__main__":
    main()

