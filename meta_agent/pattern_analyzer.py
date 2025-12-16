"""
Pattern Analyzer for Meta-Agent System

Analyzes agent traces to identify:
- Successful problem-solving patterns
- Common failure modes
- Tool usage effectiveness
- Performance optimization opportunities
"""

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .trace_collector import AgentTrace, AgentType, ThinkingStep, ToolCall

logger = logging.getLogger(__name__)


class PatternType(Enum):
    SUCCESS_PATTERN = "success_pattern"
    FAILURE_PATTERN = "failure_pattern"
    TOOL_EFFECTIVENESS = "tool_effectiveness"
    THINKING_PATTERN = "thinking_pattern"
    PERFORMANCE_OPTIMIZATION = "performance_optimization"


@dataclass
class Pattern:
    pattern_type: PatternType
    description: str
    confidence: float
    frequency: int
    examples: List[Dict[str, Any]]
    recommendations: List[str]


class PatternAnalyzer:
    """Analyzes agent traces to identify patterns and insights"""

    def __init__(self):
        self.patterns: List[Pattern] = []

    def analyze_traces(self, traces: List[AgentTrace]) -> List[Pattern]:
        """Analyze traces and identify patterns"""
        self.patterns = []

        if not traces:
            logger.warning("No traces provided for analysis")
            return self.patterns

        # Analyze different aspects
        self._analyze_success_patterns(traces)
        self._analyze_failure_patterns(traces)
        self._analyze_tool_effectiveness(traces)
        self._analyze_thinking_patterns(traces)
        self._analyze_performance_patterns(traces)

        logger.info(f"Identified {len(self.patterns)} patterns from {len(traces)} traces")
        return self.patterns

    def _analyze_success_patterns(self, traces: List[AgentTrace]) -> None:
        """Identify patterns in successful traces"""
        successful_traces = [t for t in traces if t.success]

        if not successful_traces:
            return

        # Analyze tool sequences in successful traces
        success_sequences = []
        for trace in successful_traces:
            sequence = []
            for tc in trace.tool_calls:
                if hasattr(tc, "tool_name"):
                    sequence.append(tc.tool_name)
                elif isinstance(tc, dict) and "tool_name" in tc:
                    sequence.append(tc["tool_name"])
            success_sequences.append(sequence)

        # Find common successful patterns
        common_patterns = self._find_common_sequences(success_sequences, min_frequency=2)

        for pattern, frequency in common_patterns:
            confidence = min(frequency / len(successful_traces), 1.0)

            self.patterns.append(
                Pattern(
                    pattern_type=PatternType.SUCCESS_PATTERN,
                    description=f"Successful tool sequence: {' -> '.join(pattern)}",
                    confidence=confidence,
                    frequency=frequency,
                    examples=self._get_examples_for_pattern(successful_traces, pattern),
                    recommendations=[
                        f"Consider using the sequence {' -> '.join(pattern)} for similar problems",
                        "This pattern has shown high success rate in past executions",
                    ],
                )
            )

    def _analyze_failure_patterns(self, traces: List[AgentTrace]) -> None:
        """Identify patterns in failed traces"""
        failed_traces = [t for t in traces if not t.success]

        if not failed_traces:
            return

        # Analyze common failure points
        failure_points = defaultdict(int)
        for trace in failed_traces:
            for i, tool_call in enumerate(trace.tool_calls):
                tool_success = False
                tool_name = "unknown"

                if hasattr(tool_call, "success") and hasattr(tool_call, "tool_name"):
                    tool_success = tool_call.success
                    tool_name = tool_call.tool_name
                elif isinstance(tool_call, dict):
                    tool_success = tool_call.get("success", True)
                    tool_name = tool_call.get("tool_name", "unknown")

                if not tool_success:
                    failure_points[tool_name] += 1

        # Identify most common failure points
        for tool_name, count in sorted(failure_points.items(), key=lambda x: x[1], reverse=True)[:3]:
            confidence = min(count / len(failed_traces), 1.0)

            self.patterns.append(
                Pattern(
                    pattern_type=PatternType.FAILURE_PATTERN,
                    description=f"Common failure point: {tool_name}",
                    confidence=confidence,
                    frequency=count,
                    examples=self._get_failure_examples(failed_traces, tool_name),
                    recommendations=[
                        f"Review usage of {tool_name} tool",
                        "Consider adding error handling or validation before calling this tool",
                        "Check if tool parameters are correctly formatted",
                    ],
                )
            )

    def _analyze_tool_effectiveness(self, traces: List[AgentTrace]) -> None:
        """Analyze effectiveness of different tools"""
        tool_stats = defaultdict(lambda: {"total_calls": 0, "successful_calls": 0, "avg_duration": 0})

        for trace in traces:
            for tool_call in trace.tool_calls:
                tool_name = "unknown"
                tool_success = False
                tool_duration = 0.0

                if hasattr(tool_call, "tool_name") and hasattr(tool_call, "success") and hasattr(tool_call, "duration"):
                    tool_name = tool_call.tool_name
                    tool_success = tool_call.success
                    tool_duration = tool_call.duration
                elif isinstance(tool_call, dict):
                    tool_name = tool_call.get("tool_name", "unknown")
                    tool_success = tool_call.get("success", True)
                    tool_duration = tool_call.get("duration", 0.0)

                tool_stats[tool_name]["total_calls"] += 1
                if tool_success:
                    tool_stats[tool_name]["successful_calls"] += 1
                tool_stats[tool_name]["avg_duration"] += tool_duration

        # Calculate effectiveness metrics
        for tool_name, stats in tool_stats.items():
            if stats["total_calls"] > 0:
                success_rate = stats["successful_calls"] / stats["total_calls"]
                avg_duration = stats["avg_duration"] / stats["total_calls"]

                # Identify highly effective tools
                if success_rate > 0.8 and stats["total_calls"] >= 3:
                    self.patterns.append(
                        Pattern(
                            pattern_type=PatternType.TOOL_EFFECTIVENESS,
                            description=f"Highly effective tool: {tool_name}",
                            confidence=success_rate,
                            frequency=stats["total_calls"],
                            examples=[{"tool": tool_name, "success_rate": success_rate, "avg_duration": avg_duration}],
                            recommendations=[
                                f"Prioritize using {tool_name} when appropriate",
                                f"This tool has {success_rate:.1%} success rate",
                            ],
                        )
                    )

                # Identify problematic tools
                elif success_rate < 0.5 and stats["total_calls"] >= 3:
                    self.patterns.append(
                        Pattern(
                            pattern_type=PatternType.TOOL_EFFECTIVENESS,
                            description=f"Problematic tool: {tool_name}",
                            confidence=1.0 - success_rate,
                            frequency=stats["total_calls"],
                            examples=[{"tool": tool_name, "success_rate": success_rate, "avg_duration": avg_duration}],
                            recommendations=[
                                f"Use {tool_name} with caution",
                                "Consider alternative approaches or additional validation",
                                f"Current success rate is only {success_rate:.1%}",
                            ],
                        )
                    )

    def _analyze_thinking_patterns(self, traces: List[AgentTrace]) -> None:
        """Analyze thinking patterns and reasoning quality"""
        thinking_quality = defaultdict(list)

        for trace in traces:
            for thinking in trace.thinking_steps:
                # Analyze reasoning length and complexity
                reasoning_length = len(thinking.reasoning.split())
                tool_choice = thinking.tool_choice

                thinking_quality[tool_choice].append(
                    {"length": reasoning_length, "success": trace.success, "reasoning": thinking.reasoning}
                )

        # Identify effective thinking patterns
        for tool_choice, examples in thinking_quality.items():
            if len(examples) < 3:
                continue

            successful_examples = [ex for ex in examples if ex["success"]]
            if not successful_examples:
                continue

            # Analyze reasoning characteristics of successful cases
            successful_lengths = [ex["length"] for ex in successful_examples]
            avg_successful_length = sum(successful_lengths) / len(successful_lengths)

            # Check if there's a pattern in reasoning quality
            if avg_successful_length > 20:  # More detailed reasoning
                self.patterns.append(
                    Pattern(
                        pattern_type=PatternType.THINKING_PATTERN,
                        description=f"Detailed reasoning improves success for {tool_choice}",
                        confidence=len(successful_examples) / len(examples),
                        frequency=len(examples),
                        examples=successful_examples[:3],  # Top 3 examples
                        recommendations=[
                            f"Encourage detailed reasoning when choosing {tool_choice}",
                            "Consider adding prompts that require more thorough analysis",
                            f"Average reasoning length in successful cases: {avg_successful_length:.1f} words",
                        ],
                    )
                )

    def _analyze_performance_patterns(self, traces: List[AgentTrace]) -> None:
        """Analyze performance patterns and optimization opportunities"""
        performance_data = []

        for trace in traces:
            if trace.performance_metrics:
                performance_data.append(
                    {
                        "duration": trace.performance_metrics.get("total_duration", 0),
                        "tool_calls": trace.performance_metrics.get("tool_call_count", 0),
                        "success": trace.success,
                        "agent_type": trace.agent_type.value,
                    }
                )

        if not performance_data:
            return

        # Analyze performance by agent type
        agent_performance = defaultdict(list)
        for data in performance_data:
            agent_performance[data["agent_type"]].append(data)

        for agent_type, data_list in agent_performance.items():
            if len(data_list) < 3:
                continue

            successful_data = [d for d in data_list if d["success"]]
            if not successful_data:
                continue

            # Calculate performance metrics
            avg_duration = sum(d["duration"] for d in successful_data) / len(successful_data)
            avg_tool_calls = sum(d["tool_calls"] for d in successful_data) / len(successful_data)

            # Identify optimization opportunities
            if avg_tool_calls > 10:  # High tool usage
                self.patterns.append(
                    Pattern(
                        pattern_type=PatternType.PERFORMANCE_OPTIMIZATION,
                        description=f"High tool usage in {agent_type} agent",
                        confidence=0.8,
                        frequency=len(successful_data),
                        examples=[
                            {"agent_type": agent_type, "avg_tool_calls": avg_tool_calls, "avg_duration": avg_duration}
                        ],
                        recommendations=[
                            f"Consider optimizing {agent_type} agent to reduce tool calls",
                            "Look for opportunities to combine multiple tool calls",
                            f"Current average: {avg_tool_calls:.1f} tool calls per successful execution",
                        ],
                    )
                )

    def _find_common_sequences(self, sequences: List[List[str]], min_frequency: int = 2) -> List[Tuple[List[str], int]]:
        """Find common subsequences in tool call sequences"""
        sequence_counts = Counter()

        for sequence in sequences:
            # Find all subsequences of length 2-5
            for length in range(2, min(6, len(sequence) + 1)):
                for i in range(len(sequence) - length + 1):
                    subsequence = tuple(sequence[i : i + length])
                    sequence_counts[subsequence] += 1

        # Return sequences that meet minimum frequency
        return [(list(seq), count) for seq, count in sequence_counts.most_common() if count >= min_frequency]

    def _get_examples_for_pattern(self, traces: List[AgentTrace], pattern: List[str]) -> List[Dict[str, Any]]:
        """Get examples of traces that contain the given pattern"""
        examples = []

        for trace in traces:
            tool_sequence = [tc.tool_name for tc in trace.tool_calls]
            if self._sequence_contains_pattern(tool_sequence, pattern):
                examples.append(
                    {
                        "trace_id": trace.trace_id,
                        "problem_id": trace.problem_context.problem_id,
                        "success": trace.success,
                        "tool_sequence": tool_sequence,
                    }
                )
                if len(examples) >= 3:  # Limit examples
                    break

        return examples

    def _get_failure_examples(self, traces: List[AgentTrace], tool_name: str) -> List[Dict[str, Any]]:
        """Get examples of failed traces involving the given tool"""
        examples = []

        for trace in traces:
            for tool_call in trace.tool_calls:
                if tool_call.tool_name == tool_name and not tool_call.success:
                    examples.append(
                        {
                            "trace_id": trace.trace_id,
                            "problem_id": trace.problem_context.problem_id,
                            "tool_call": {
                                "tool_name": tool_call.tool_name,
                                "arguments": tool_call.arguments,
                                "response": tool_call.response,
                            },
                        }
                    )
                    if len(examples) >= 3:  # Limit examples
                        break
            if len(examples) >= 3:
                break

        return examples

    def _sequence_contains_pattern(self, sequence: List[str], pattern: List[str]) -> bool:
        """Check if a sequence contains a pattern (consecutive subsequence)"""
        if len(pattern) > len(sequence):
            return False

        for i in range(len(sequence) - len(pattern) + 1):
            if sequence[i : i + len(pattern)] == pattern:
                return True
        return False

    def get_patterns_by_type(self, pattern_type: PatternType) -> List[Pattern]:
        """Get patterns filtered by type"""
        return [p for p in self.patterns if p.pattern_type == pattern_type]

    def get_high_confidence_patterns(self, min_confidence: float = 0.7) -> List[Pattern]:
        """Get patterns with high confidence scores"""
        return [p for p in self.patterns if p.confidence >= min_confidence]
