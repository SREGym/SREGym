"""
Trace Collector for Meta-Agent System

Collects and stores agent execution traces, including:
- Tool usage patterns
- Problem-solving strategies
- Success/failure outcomes
- Performance metrics
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentType(Enum):
    DIAGNOSIS = "diagnosis"
    LOCALIZATION = "localization"
    MITIGATION = "mitigation"
    ROLLBACK = "rollback"


class TraceType(Enum):
    TOOL_CALL = "tool_call"
    THINKING_STEP = "thinking_step"
    PROBLEM_SOLVING = "problem_solving"
    SUBMISSION = "submission"
    ERROR = "error"


@dataclass
class ToolCall:
    tool_name: str
    arguments: Dict[str, Any]
    timestamp: float
    success: bool
    response: str
    duration: float


@dataclass
class ThinkingStep:
    reasoning: str
    tool_choice: str
    justification: str
    timestamp: float


@dataclass
class ProblemContext:
    problem_id: str
    app_name: str
    app_namespace: str
    app_description: str
    fault_type: Optional[str] = None
    initial_state: Optional[Dict[str, Any]] = None


@dataclass
class AgentTrace:
    trace_id: str
    agent_type: AgentType
    problem_context: ProblemContext
    start_time: float
    end_time: Optional[float] = None
    success: bool = False
    final_submission: Optional[str] = None
    tool_calls: List[ToolCall] = None
    thinking_steps: List[ThinkingStep] = None
    performance_metrics: Dict[str, Any] = None
    ground_truth: Optional[Dict[str, Any]] = None
    oracle_results_enhanced: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []
        if self.thinking_steps is None:
            self.thinking_steps = []
        if self.performance_metrics is None:
            self.performance_metrics = {}


class TraceCollector:
    """Collects and manages agent execution traces"""

    def __init__(self, storage_dir: str = "meta_agent/traces"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.active_traces: Dict[str, AgentTrace] = {}

    def start_trace(self, trace_id: str, agent_type: AgentType, problem_context: ProblemContext) -> AgentTrace:
        """Start a new agent trace"""
        trace = AgentTrace(
            trace_id=trace_id, agent_type=agent_type, problem_context=problem_context, start_time=time.time()
        )
        self.active_traces[trace_id] = trace
        logger.info(f"Started trace {trace_id} for {agent_type.value} agent")
        return trace

    def add_tool_call(
        self, trace_id: str, tool_name: str, arguments: Dict[str, Any], success: bool, response: str, duration: float
    ) -> None:
        """Add a tool call to the trace"""
        if trace_id not in self.active_traces:
            logger.warning(f"Trace {trace_id} not found")
            return

        tool_call = ToolCall(
            tool_name=tool_name,
            arguments=arguments,
            timestamp=time.time(),
            success=success,
            response=response,
            duration=duration,
        )
        self.active_traces[trace_id].tool_calls.append(tool_call)

    def add_thinking_step(self, trace_id: str, reasoning: str, tool_choice: str, justification: str) -> None:
        """Add a thinking step to the trace"""
        if trace_id not in self.active_traces:
            logger.warning(f"Trace {trace_id} not found")
            return

        thinking_step = ThinkingStep(
            reasoning=reasoning, tool_choice=tool_choice, justification=justification, timestamp=time.time()
        )
        self.active_traces[trace_id].thinking_steps.append(thinking_step)

    def add_performance_metric(self, trace_id: str, metric_name: str, value: Any) -> None:
        """Add a performance metric to the trace"""
        if trace_id not in self.active_traces:
            logger.warning(f"Trace {trace_id} not found")
            return

        self.active_traces[trace_id].performance_metrics[metric_name] = value

    def get_trace(self, trace_id: str) -> Optional[AgentTrace]:
        """Get an active trace by ID"""
        return self.active_traces.get(trace_id)

    def end_trace(
        self,
        trace_id: str,
        success: bool,
        final_submission: Optional[str] = None,
        ground_truth: Optional[Dict[str, Any]] = None,
        oracle_results: Optional[Dict[str, Any]] = None,
    ) -> AgentTrace:
        """End a trace and save it
        
        Args:
            trace_id: Trace ID
            success: Whether the trace was successful
            final_submission: Final submission content
            ground_truth: Ground truth expectations for this problem
            oracle_results: Oracle evaluation results from conductor
        """
        if trace_id not in self.active_traces:
            logger.warning(f"Trace {trace_id} not found")
            return None

        trace = self.active_traces[trace_id]
        trace.end_time = time.time()
        trace.success = success
        trace.final_submission = final_submission

        # Store ground truth and enhanced oracle results
        if ground_truth:
            trace.ground_truth = ground_truth

        if oracle_results and ground_truth:
            trace.oracle_results_enhanced = self._enhance_oracle_results(oracle_results, ground_truth, trace.agent_type, trace)

        # Calculate additional metrics
        trace.performance_metrics.update(
            {
                "total_duration": trace.end_time - trace.start_time,
                "tool_call_count": len(trace.tool_calls),
                "thinking_step_count": len(trace.thinking_steps),
                "success_rate": sum(1 for tc in trace.tool_calls if tc.success) / max(len(trace.tool_calls), 1),
            }
        )

        # Add accuracy score if available
        if trace.oracle_results_enhanced:
            if trace.agent_type == AgentType.LOCALIZATION:
                loc_result = trace.oracle_results_enhanced.get("localization", {})
                trace.performance_metrics["accuracy_score"] = loc_result.get("accuracy", 0.0)
                trace.performance_metrics["partial_credit"] = loc_result.get("partial_credit", False)
            elif trace.agent_type == AgentType.DIAGNOSIS:
                det_result = trace.oracle_results_enhanced.get("detection", {})
                trace.performance_metrics["accuracy_score"] = det_result.get("accuracy", 0.0)

        # Save trace
        self._save_trace(trace)

        # Remove from active traces
        del self.active_traces[trace_id]

        logger.info(f"Ended trace {trace_id} - Success: {success}")
        return trace

    def _enhance_oracle_results(
        self, oracle_results: Dict[str, Any], ground_truth: Dict[str, Any], agent_type: AgentType, trace: Optional[AgentTrace] = None
    ) -> Dict[str, Any]:
        """Enhance oracle results with ground truth comparison"""
        enhanced = {}

        # Handle detection
        if "Detection" in oracle_results and "detection" in ground_truth:
            det_result = oracle_results["Detection"]
            det_gt = ground_truth["detection"]
            # Try to extract actual submission from trace
            actual = None
            if trace and trace.final_submission:
                actual = trace.final_submission.strip()
            enhanced["detection"] = {
                "expected": det_gt.get("expected"),
                "actual": actual,
                "success": det_result.get("success", False),
                "accuracy": det_result.get("accuracy", 0.0),
            }

        # Handle localization
        if "Localization" in oracle_results and "localization" in ground_truth:
            loc_result = oracle_results["Localization"]
            loc_gt = ground_truth["localization"]
            expected_services = loc_gt.get("expected", [])
            if isinstance(expected_services, str):
                expected_services = [expected_services]
            
            # Extract submitted services from trace's final_submission or result
            submitted_services = []
            if trace and trace.final_submission:
                # Try to parse from final_submission
                submission = trace.final_submission.strip()
                if "," in submission:
                    submitted_services = [s.strip() for s in submission.split(",")]
                elif submission:
                    submitted_services = [submission]
            elif "submitted" in loc_result:
                submitted = loc_result["submitted"]
                if isinstance(submitted, str):
                    submitted_services = [s.strip() for s in submitted.split(",")]
                elif isinstance(submitted, list):
                    submitted_services = [str(s) for s in submitted]
            
            # Calculate missing and extra services
            expected_set = set(str(s).lower() for s in expected_services)
            submitted_set = set(str(s).lower() for s in submitted_services)
            missing = [s for s in expected_services if str(s).lower() not in submitted_set]
            extra = [s for s in submitted_services if str(s).lower() not in expected_set]

            enhanced["localization"] = {
                "expected": expected_services,
                "submitted": submitted_services,
                "missing": missing,
                "extra": extra,
                "accuracy": loc_result.get("accuracy", 0.0),
                "partial_credit": loc_result.get("is_subset", False),
                "services_identified": len(submitted_services),
                "services_expected": len(expected_services),
            }

        # Handle mitigation
        if "Mitigation" in oracle_results and "mitigation" in ground_truth:
            mit_result = oracle_results["Mitigation"]
            mit_gt = ground_truth["mitigation"]
            enhanced["mitigation"] = {
                "expected_checks": mit_gt.get("description", ""),
                "oracle_type": mit_gt.get("oracle_type", ""),
                "oracle_results": mit_result,
                "accuracy": mit_result.get("accuracy", 0.0),
                "success": mit_result.get("success", False),
            }
            if "sub_oracles" in mit_gt:
                enhanced["mitigation"]["expected_sub_oracles"] = mit_gt["sub_oracles"]

        return enhanced

    def _save_trace(self, trace: AgentTrace) -> None:
        """Save trace to storage"""
        timestamp = datetime.fromtimestamp(trace.start_time).strftime("%Y%m%d_%H%M%S")

        # Handle both ProblemContext object and dict formats
        if isinstance(trace.problem_context, dict):
            problem_id = trace.problem_context.get("problem_id", "unknown")
        else:
            problem_id = trace.problem_context.problem_id

        filename = f"{trace.agent_type.value}_{problem_id}_{timestamp}_{trace.trace_id}.json"
        filepath = self.storage_dir / filename

        with open(filepath, "w") as f:
            json.dump(asdict(trace), f, indent=2, default=str)

        logger.info(f"Saved trace to {filepath}")

    def load_traces(
        self,
        agent_type: Optional[AgentType] = None,
        problem_id: Optional[str] = None,
        limit: Optional[int] = None,
        include_historical: bool = False,
    ) -> List[AgentTrace]:
        """Load traces from storage with optional filtering"""
        traces = []

        for filepath in self.storage_dir.glob("*.json"):
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)

                # Skip historical traces if include_historical is False
                if not include_historical:
                    # When using run-specific folders, all traces in the folder are from current run
                    # No additional filtering needed since each run has its own folder
                    pass

                # Convert back to AgentTrace object
                # Handle the case where agent_type might be a string
                if isinstance(data.get("agent_type"), str):
                    try:
                        data["agent_type"] = AgentType(data["agent_type"])
                    except ValueError:
                        # If it's already an enum value, try to convert it
                        if "DIAGNOSIS" in data["agent_type"]:
                            data["agent_type"] = AgentType.DIAGNOSIS
                        elif "LOCALIZATION" in data["agent_type"]:
                            data["agent_type"] = AgentType.LOCALIZATION
                        elif "MITIGATION" in data["agent_type"]:
                            data["agent_type"] = AgentType.MITIGATION
                        elif "ROLLBACK" in data["agent_type"]:
                            data["agent_type"] = AgentType.ROLLBACK
                        else:
                            data["agent_type"] = AgentType.DIAGNOSIS

                # Convert problem_context if it exists
                if "problem_context" in data and isinstance(data["problem_context"], dict):
                    data["problem_context"] = ProblemContext(**data["problem_context"])

                # Convert tool_calls and thinking_steps if they exist
                if "tool_calls" in data and data["tool_calls"]:
                    tool_calls = []
                    for tc in data["tool_calls"]:
                        if isinstance(tc, dict):
                            tool_calls.append(ToolCall(**tc))
                        else:
                            tool_calls.append(tc)
                    data["tool_calls"] = tool_calls

                if "thinking_steps" in data and data["thinking_steps"]:
                    thinking_steps = []
                    for ts in data["thinking_steps"]:
                        if isinstance(ts, dict):
                            thinking_steps.append(ThinkingStep(**ts))
                        else:
                            thinking_steps.append(ts)
                    data["thinking_steps"] = thinking_steps

                trace = AgentTrace(**data)

                # Apply filters
                if agent_type and trace.agent_type != agent_type:
                    continue
                if problem_id and trace.problem_context.problem_id != problem_id:
                    continue

                traces.append(trace)

            except Exception as e:
                logger.warning(f"Failed to load trace from {filepath}: {e}")
                continue

        # Sort by start time (newest first)
        traces.sort(key=lambda x: x.start_time, reverse=True)

        if limit:
            traces = traces[:limit]

        return traces

    def get_trace_statistics(
        self, agent_type: Optional[AgentType] = None, problem_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get statistics about collected traces"""
        traces = self.load_traces(agent_type, problem_id)

        if not traces:
            return {"total_traces": 0}

        successful_traces = [t for t in traces if t.success]

        return {
            "total_traces": len(traces),
            "successful_traces": len(successful_traces),
            "success_rate": len(successful_traces) / len(traces),
            "avg_duration": sum(t.performance_metrics.get("total_duration", 0) for t in traces) / len(traces),
            "avg_tool_calls": sum(t.performance_metrics.get("tool_call_count", 0) for t in traces) / len(traces),
            "most_used_tools": self._get_most_used_tools(traces),
            "common_failure_patterns": self._get_failure_patterns(traces),
        }

    def _get_most_used_tools(self, traces: List[AgentTrace]) -> Dict[str, int]:
        """Get most frequently used tools across traces"""
        tool_usage = {}
        for trace in traces:
            for tool_call in trace.tool_calls:
                if hasattr(tool_call, "tool_name"):
                    tool_usage[tool_call.tool_name] = tool_usage.get(tool_call.tool_name, 0) + 1
                elif isinstance(tool_call, dict) and "tool_name" in tool_call:
                    tool_usage[tool_call["tool_name"]] = tool_usage.get(tool_call["tool_name"], 0) + 1
        return dict(sorted(tool_usage.items(), key=lambda x: x[1], reverse=True))

    def _get_failure_patterns(self, traces: List[AgentTrace]) -> List[Dict[str, Any]]:
        """Identify common patterns in failed traces"""
        failed_traces = [t for t in traces if not t.success]
        patterns = []

        # Analyze tool call patterns in failed traces
        failed_tool_sequences = []
        for trace in failed_traces:
            sequence = [tc.tool_name for tc in trace.tool_calls]
            failed_tool_sequences.append(sequence)

        # Find common prefixes in failed sequences
        if failed_tool_sequences:
            common_prefixes = self._find_common_prefixes(failed_tool_sequences)
            patterns.extend([{"type": "common_failed_sequence", "pattern": prefix} for prefix in common_prefixes])

        return patterns

    def _find_common_prefixes(self, sequences: List[List[str]], min_length: int = 2) -> List[List[str]]:
        """Find common prefixes in sequences of tool calls"""
        if not sequences:
            return []

        # Find all possible prefixes
        all_prefixes = set()
        for sequence in sequences:
            for i in range(min_length, len(sequence) + 1):
                all_prefixes.add(tuple(sequence[:i]))

        # Count occurrences
        prefix_counts = {}
        for prefix in all_prefixes:
            count = sum(1 for seq in sequences if tuple(seq[: len(prefix)]) == prefix)
            if count > 1:  # Only consider prefixes that appear in multiple sequences
                prefix_counts[prefix] = count

        # Return most common prefixes
        return [list(prefix) for prefix, count in sorted(prefix_counts.items(), key=lambda x: x[1], reverse=True)[:5]]
