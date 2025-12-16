"""
LLM-Based Optimizer for Meta-Agent System

Uses an LLM (e.g., Gemini Pro Flash) to optimize agent prompts and configurations
based on execution traces and reward specifications (success, latency, num_attempts).
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools

from .trace_collector import AgentTrace, AgentType

logger = logging.getLogger(__name__)


@dataclass
class RewardSpec:
    """Reward specification for optimization objectives"""

    success_weight: float = 2.0  # Weight for success rate (higher is better) - INCREASED to prioritize success
    latency_weight: float = -0.3  # Weight for latency (negative because lower is better) - REDUCED penalty
    attempts_weight: float = -0.2  # Weight for number of attempts (negative because lower is better) - REDUCED penalty

    def compute_reward(self, success: bool, latency: float, num_attempts: int, success_rate: float = None) -> float:
        """
        Compute reward score based on metrics
        
        Args:
            success: Whether this individual trace succeeded
            latency: Latency in milliseconds
            num_attempts: Number of tool attempts
            success_rate: Optional overall success rate (for conditional optimization)
        """
        success_score = 1.0 if success else 0.0
        
        # If overall success rate is provided, conditionally optimize latency/attempts
        # Only optimize speed if success rate is high enough
        if success_rate is not None:
            if success_rate < 0.9:  # If success rate < 90%, prioritize success only
                latency_weight = 0.0
                attempts_weight = 0.0
            else:
                latency_weight = self.latency_weight
                attempts_weight = self.attempts_weight
        else:
            latency_weight = self.latency_weight
            attempts_weight = self.attempts_weight
        
        return (
            self.success_weight * success_score
            + latency_weight * latency / 1000.0  # Normalize latency (convert to seconds, scale down)
            + attempts_weight * num_attempts
        )


class LLMPromptOptimizer:
    """Uses LLM to optimize agent prompts based on traces and rewards"""

    def __init__(self, model_name: str = "gemini/gemini-2.5-flash", reward_spec: Optional[RewardSpec] = None):
        """
        Initialize LLM prompt optimizer

        Args:
            model_name: LLM model to use (default: gemini/gemini-2.5-flash)
            reward_spec: Reward specification for optimization objectives
        """
        self.model_name = model_name
        self.reward_spec = reward_spec or RewardSpec()
        self.llm_backend = None
        self._initialize_llm()

    def _initialize_llm(self):
        """Initialize LLM backend"""
        try:
            # Set environment variables for Gemini
            import os

            from dotenv import load_dotenv

            # Load .env file to get API keys
            load_dotenv()

            # Try to use existing backend initialization
            # We'll use a custom initialization for the meta-agent
            from clients.stratus.llm_backend.get_llm_backend import LiteLLMBackend

            # Get API key from environment
            api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
            if not api_key:
                logger.warning("No GOOGLE_API_KEY or GEMINI_API_KEY found. LLM optimizer may not work.")

            self.llm_backend = LiteLLMBackend(
                provider="litellm",
                model_name=self.model_name,
                url="",
                api_key=api_key or "",
                api_version="",
                seed=42,
                top_p=0.95,
                temperature=0.7,  # Slightly higher temperature for creative optimization
                reasoning_effort="",
                thinking_tools="",
                thinking_budget_tools=0,
                max_tokens=8000,
            )
            logger.info(f"Initialized LLM optimizer with model: {self.model_name}")
        except Exception as e:
            logger.error(f"Failed to initialize LLM backend: {e}")
            self.llm_backend = None

    def optimize_prompt(
        self,
        agent_type: AgentType,
        current_prompt: Dict[str, Any],
        traces: List[AgentTrace],
        reward_spec: Optional[RewardSpec] = None,
        existing_insights: Optional[List[Dict[str, Any]]] = None,
        max_retries: int = 3,
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Optimize agent prompt using LLM based on traces and reward specification with retry logic

        Args:
            agent_type: Type of agent to optimize
            current_prompt: Current prompt configuration (YAML structure)
            traces: List of execution traces for this agent
            reward_spec: Reward specification (uses instance default if not provided)
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            Tuple of (optimized prompt configuration, success: bool)
        """
        if not self.llm_backend:
            logger.error("LLM backend not initialized. Cannot optimize prompt.")
            return current_prompt, False

        reward_spec = reward_spec or self.reward_spec

        # Analyze traces and compute metrics
        trace_analysis = self._analyze_traces(traces, reward_spec)
        
        # Store traces for use in formatting (needed for ground truth examples)
        trace_analysis["_traces"] = traces

        # Build optimization prompt for LLM (include existing insights for deduplication)
        optimization_prompt = self._build_optimization_prompt(
            agent_type, current_prompt, trace_analysis, reward_spec, existing_insights or []
        )

        # Retry logic
        for attempt in range(1, max_retries + 1):
            try:
                # Call LLM for optimization
                logger.info(
                    f"Requesting LLM optimization for {agent_type.value} agent (attempt {attempt}/{max_retries})..."
                )
                response = self.llm_backend.inference(messages=[optimization_prompt], system_prompt=None)

                # Parse LLM response
                optimized_prompt, success = self._parse_llm_response(response, current_prompt)

                if success:
                    logger.info(f"Successfully optimized prompt for {agent_type.value} agent on attempt {attempt}")
                    return optimized_prompt, True
                else:
                    if attempt < max_retries:
                        logger.warning(
                            f"LLM optimization failed for {agent_type.value} agent on attempt {attempt}, retrying..."
                        )
                    else:
                        logger.error(
                            f"LLM optimization failed for {agent_type.value} agent after {max_retries} attempts"
                        )
                        return current_prompt, False

            except Exception as e:
                logger.error(f"Error during LLM optimization (attempt {attempt}/{max_retries}): {e}")
                if attempt >= max_retries:
                    return current_prompt, False
                # Continue to next retry

        # Should not reach here, but return current prompt if we do
        return current_prompt, False

    def _analyze_traces(self, traces: List[AgentTrace], reward_spec: RewardSpec) -> Dict[str, Any]:
        """Analyze traces to extract metrics and patterns"""
        if not traces:
            return {
                "total_traces": 0,
                "success_rate": 0.0,
                "avg_latency": 0.0,
                "avg_attempts": 0.0,
                "reward_score": 0.0,
                "successful_traces": [],
                "failed_traces": [],
                "common_patterns": [],
            }

        successful_traces = [t for t in traces if t.success]
        failed_traces = [t for t in traces if not t.success]

        # Compute metrics
        success_rate = len(successful_traces) / len(traces) if traces else 0.0

        latencies = []
        num_attempts = []

        for trace in traces:
            if trace.end_time and trace.start_time:
                latency = trace.end_time - trace.start_time
                latencies.append(latency)

            if trace.performance_metrics:
                attempts = trace.performance_metrics.get("tool_call_count", 0)
                num_attempts.append(attempts)

        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        avg_attempts = sum(num_attempts) / len(num_attempts) if num_attempts else 0.0

        # Compute overall reward score
        reward_scores = []
        for trace in traces:
            latency = trace.end_time - trace.start_time if (trace.end_time and trace.start_time) else 0.0
            attempts = trace.performance_metrics.get("tool_call_count", 0) if trace.performance_metrics else 0
            reward_scores.append(reward_spec.compute_reward(trace.success, latency, attempts, success_rate))

        avg_reward = sum(reward_scores) / len(reward_scores) if reward_scores else 0.0

        # Extract common patterns from successful vs failed traces
        successful_patterns = self._extract_patterns(successful_traces)
        failed_patterns = self._extract_patterns(failed_traces)

        # Extract ground truth analysis
        ground_truth_analysis = self._analyze_ground_truth(traces)
        
        # Extract problem-specific failure analysis
        problem_specific_analysis = self._analyze_problem_specific_failures(traces)

        return {
            "total_traces": len(traces),
            "success_rate": success_rate,
            "avg_latency": avg_latency,
            "avg_attempts": avg_attempts,
            "reward_score": avg_reward,
            "successful_traces": len(successful_traces),
            "failed_traces": len(failed_traces),
            "successful_patterns": successful_patterns,
            "failed_patterns": failed_patterns,
            "ground_truth_analysis": ground_truth_analysis,
            "problem_specific_analysis": problem_specific_analysis,
        }

    def _analyze_ground_truth(self, traces: List[AgentTrace]) -> Dict[str, Any]:
        """Analyze traces with ground truth context"""
        if not traces:
            return {}

        analysis = {
            "localization": {},
            "detection": {},
            "mitigation": {},
        }

        # Analyze localization traces
        localization_traces = [t for t in traces if t.agent_type.value == "localization"]
        if localization_traces:
            loc_failed = [t for t in localization_traces if not t.success and t.oracle_results_enhanced]
            loc_successful = [t for t in localization_traces if t.success and t.oracle_results_enhanced]

            if loc_failed:
                # Analyze missing services
                missing_services_all = []
                partial_credit_count = 0
                avg_accuracy = 0.0
                for trace in loc_failed:
                    loc_result = trace.oracle_results_enhanced.get("localization", {})
                    missing = loc_result.get("missing", [])
                    missing_services_all.extend(missing)
                    if loc_result.get("partial_credit", False):
                        partial_credit_count += 1
                    avg_accuracy += loc_result.get("accuracy", 0.0)

                from collections import Counter

                missing_counter = Counter(missing_services_all)
                common_missing = [s for s, count in missing_counter.most_common(5)]

                analysis["localization"] = {
                    "failed_count": len(loc_failed),
                    "common_missing_services": common_missing,
                    "missing_counts": dict(missing_counter),
                    "partial_credit_count": partial_credit_count,
                    "avg_failed_accuracy": avg_accuracy / len(loc_failed) if loc_failed else 0.0,
                    "successful_count": len(loc_successful),
                }

        # Analyze detection traces
        detection_traces = [t for t in traces if t.agent_type.value == "diagnosis"]
        if detection_traces:
            det_failed = [t for t in detection_traces if not t.success and t.oracle_results_enhanced]
            analysis["detection"] = {
                "failed_count": len(det_failed),
                "successful_count": len([t for t in detection_traces if t.success]),
            }

        return analysis

    def _analyze_problem_specific_failures(self, traces: List[AgentTrace]) -> Dict[str, Any]:
        """Analyze failures by problem type to identify problem-specific patterns"""
        from collections import defaultdict
        
        if not traces:
            return {}
        
        # Group traces by problem ID and agent type
        problem_failures = defaultdict(lambda: defaultdict(lambda: {"count": 0, "traces": []}))
        
        for trace in traces:
            if trace.problem_context and not trace.success:
                problem_id = trace.problem_context.problem_id
                agent_type = trace.agent_type.value
                
                problem_failures[problem_id][agent_type]["count"] += 1
                problem_failures[problem_id][agent_type]["traces"].append({
                    "trace_id": trace.trace_id,
                    "tool_calls": [tc.tool_name if hasattr(tc, "tool_name") else str(tc) for tc in trace.tool_calls[:5]],
                    "final_submission": trace.final_submission[:200] if trace.final_submission else None,
                })
        
        # Format for LLM prompt
        formatted = {}
        for problem_id, agent_failures in problem_failures.items():
            formatted[problem_id] = {}
            for agent_type, failure_info in agent_failures.items():
                formatted[problem_id][agent_type] = {
                    "failure_count": failure_info["count"],
                    "sample_traces": failure_info["traces"][:2],  # Limit to 2 samples
                }
        
        return formatted

    def _extract_patterns(self, traces: List[AgentTrace]) -> List[Dict[str, Any]]:
        """Extract high-level patterns from traces"""
        patterns = []

        if not traces:
            return patterns

        # Extract tool usage patterns
        tool_usage = {}
        for trace in traces:
            for tool_call in trace.tool_calls:
                tool_name = (
                    tool_call.tool_name if hasattr(tool_call, "tool_name") else tool_call.get("tool_name", "unknown")
                )
                if tool_name not in tool_usage:
                    tool_usage[tool_name] = {"count": 0, "success_count": 0}
                tool_usage[tool_name]["count"] += 1
                success = tool_call.success if hasattr(tool_call, "success") else tool_call.get("success", True)
                if success:
                    tool_usage[tool_name]["success_count"] += 1

        # Find most common tools
        for tool_name, stats in sorted(tool_usage.items(), key=lambda x: x[1]["count"], reverse=True)[:5]:
            success_rate = stats["success_count"] / stats["count"] if stats["count"] > 0 else 0.0
            patterns.append(
                {"type": "tool_usage", "tool": tool_name, "frequency": stats["count"], "success_rate": success_rate}
            )

        # Extract reasoning patterns from thinking steps
        reasoning_lengths = []
        for trace in traces:
            for thinking in trace.thinking_steps:
                reasoning = thinking.reasoning if hasattr(thinking, "reasoning") else thinking.get("reasoning", "")
                reasoning_lengths.append(len(reasoning.split()))

        if reasoning_lengths:
            avg_reasoning_length = sum(reasoning_lengths) / len(reasoning_lengths)
            patterns.append(
                {
                    "type": "reasoning",
                    "avg_reasoning_length": avg_reasoning_length,
                    "sample_count": len(reasoning_lengths),
                }
            )

        return patterns

    def _format_ground_truth_analysis(self, gt_analysis: Dict[str, Any], agent_type: AgentType, traces: List[AgentTrace] = None) -> str:
        """Format ground truth analysis for the LLM prompt with concrete examples"""
        if not gt_analysis:
            return "No ground truth data available for analysis."

        formatted = []
        
        # Get concrete examples from traces
        examples = []
        if traces:
            for trace in traces[:5]:  # Limit to 5 examples
                if trace.oracle_results_enhanced and trace.problem_context:
                    if agent_type.value == "localization" and "localization" in trace.oracle_results_enhanced:
                        loc_result = trace.oracle_results_enhanced["localization"]
                        expected = loc_result.get("expected", [])
                        actual = trace.final_submission if trace.final_submission else "No submission"
                        missing = loc_result.get("missing", [])
                        if missing:
                            examples.append({
                                "problem": trace.problem_context.problem_id,
                                "expected": expected,
                                "actual": actual,
                                "missing": missing
                            })

        # Format localization analysis
        if agent_type.value == "localization" and "localization" in gt_analysis:
            loc_analysis = gt_analysis["localization"]
            if loc_analysis:
                formatted.append("**Localization Ground Truth Analysis:**")
                formatted.append(f"- Failed traces: {loc_analysis.get('failed_count', 0)}")
                formatted.append(f"- Successful traces: {loc_analysis.get('successful_count', 0)}")
                
                if loc_analysis.get("common_missing_services"):
                    formatted.append(f"- Most commonly missing services: {', '.join(loc_analysis['common_missing_services'][:5])}")
                
                if loc_analysis.get("partial_credit_count", 0) > 0:
                    formatted.append(f"- Partial credit cases: {loc_analysis['partial_credit_count']} (identified some but not all required services)")
                
                if loc_analysis.get("avg_failed_accuracy", 0) > 0:
                    formatted.append(f"- Average accuracy in failed traces: {loc_analysis['avg_failed_accuracy']:.1f}%")
                
                formatted.append("")
                formatted.append("**Concrete Examples of Failures:**")
                for i, example in enumerate(examples[:3], 1):  # Show up to 3 examples
                    formatted.append(f"\nExample {i} - Problem: {example['problem']}")
                    formatted.append(f"  Expected services: {', '.join(example['expected'][:10])}")
                    formatted.append(f"  Agent submitted: {example['actual'][:100]}")
                    formatted.append(f"  Missing services: {', '.join(example['missing'][:5])}")
                    formatted.append(f"  Recommendation: Use 'kubectl get svc -n <namespace>' to list all services, then check pod health for each")
                
                formatted.append("")
                formatted.append("**Key Insights:**")
                if loc_analysis.get("common_missing_services"):
                    missing = loc_analysis["common_missing_services"]
                    formatted.append(f"- Agents frequently miss these services: {', '.join(missing[:3])}")
                    formatted.append("- Recommendation: Add explicit guidance to identify ALL affected services, not just the first one found")
                    formatted.append(f"- Specific validation checklist: Before submitting, verify you've checked: {', '.join(missing[:5])}")
                    formatted.append("- Workflow: 1) List all services with 'kubectl get svc', 2) Check pod health for each service, 3) Include ALL services with unhealthy pods")
                
                if loc_analysis.get("partial_credit_count", 0) > 0:
                    formatted.append("- Agents often identify only a subset of required services")
                    formatted.append("- Recommendation: Add a validation step to verify all expected services are included before submitting")

        # Format detection analysis
        elif agent_type.value == "diagnosis" and "detection" in gt_analysis:
            det_analysis = gt_analysis["detection"]
            if det_analysis:
                formatted.append("**Detection Ground Truth Analysis:**")
                formatted.append(f"- Failed traces: {det_analysis.get('failed_count', 0)}")
                formatted.append(f"- Successful traces: {det_analysis.get('successful_count', 0)}")

        return "\n".join(formatted) if formatted else "No specific ground truth insights available for this agent type."

    def _format_problem_specific_analysis(self, problem_analysis: Dict[str, Any], agent_type: AgentType) -> str:
        """Format problem-specific failure analysis for the LLM prompt"""
        if not problem_analysis:
            return "No problem-specific failure data available."
        
        formatted = []
        formatted.append("**Problem-Specific Failure Analysis:**")
        formatted.append("")
        
        agent_type_str = agent_type.value
        for problem_id, agent_failures in problem_analysis.items():
            if agent_type_str in agent_failures:
                failure_info = agent_failures[agent_type_str]
                failure_count = failure_info.get("failure_count", 0)
                if failure_count > 0:
                    formatted.append(f"**Problem: {problem_id}**")
                    formatted.append(f"- Failed {failure_count} time(s) in {agent_type_str}")
                    
                    # Provide specific guidance based on problem type
                    if "high_cpu" in problem_id.lower() or "cpu" in problem_id.lower():
                        formatted.append("- Recommendation: For CPU-related problems, check pod resource requests/limits first with 'kubectl describe pod' before checking metrics")
                    elif "network" in problem_id.lower() or "policy" in problem_id.lower():
                        formatted.append("- Recommendation: For NetworkPolicy problems, check NetworkPolicy resources in the namespace with 'kubectl get networkpolicies -n <namespace>'")
                    elif "concurrent" in problem_id.lower():
                        formatted.append("- Recommendation: For concurrent failures, check all affected app namespaces and verify each service independently")
                    
                    formatted.append("")
        
        return "\n".join(formatted) if formatted else "No specific problem failures identified for this agent type."

    def _format_existing_insights(self, existing_insights: List[Dict[str, Any]]) -> str:
        """Format existing insights for the LLM prompt to avoid duplicates"""
        if not existing_insights:
            return "No existing insights. You can generate any new insights based on the performance data."
        
        formatted = []
        formatted.append(f"**You have {len(existing_insights)} existing insights. DO NOT generate duplicates or semantically similar insights.**\n")
        formatted.append("**Review these carefully before generating new insights:**\n")
        
        for i, insight in enumerate(existing_insights, 1):
            insight_type = insight.get("type", "unknown")
            content = insight.get("content", "").strip()
            verified = insight.get("verified", False)
            status = "✅ VERIFIED" if verified else "⚠️ UNVERIFIED"
            
            formatted.append(f"\n**Existing Insight {i}** ({status}):")
            formatted.append(f"Type: {insight_type}")
            # Include first 200 chars of content to give LLM context
            content_preview = content[:200] + "..." if len(content) > 200 else content
            formatted.append(f"Content: {content_preview}")
        
        formatted.append("\n**IMPORTANT:** Before generating any new insight, check if it is semantically similar to any existing insight above. If it conveys the same meaning, recommendation, or warning, DO NOT include it.")
        
        return "\n".join(formatted)

    def _build_optimization_prompt(
        self,
        agent_type: AgentType,
        current_prompt: Dict[str, Any],
        trace_analysis: Dict[str, Any],
        reward_spec: RewardSpec,
        existing_insights: List[Dict[str, Any]] = None,
    ) -> str:
        """Build prompt for LLM optimization"""

        prompt = f"""You are an expert at optimizing AI agent prompts based on execution performance data.

## Task
Optimize the prompt for a {agent_type.value} agent in a Kubernetes SRE system. The agent is responsible for {self._get_agent_description(agent_type)}.

## Current Performance Metrics
- Total traces analyzed: {trace_analysis['total_traces']}
- Success rate: {trace_analysis['success_rate']:.2%}
- Average latency: {trace_analysis['avg_latency']:.2f} seconds
- Average number of tool calls (attempts): {trace_analysis['avg_attempts']:.1f}
- Overall reward score: {trace_analysis['reward_score']:.3f}

## Reward Specification (what to optimize for)
- Success weight: {reward_spec.success_weight} (higher is better)
- Latency weight: {reward_spec.latency_weight} (negative = lower is better)
- Attempts weight: {reward_spec.attempts_weight} (negative = lower is better)

## Current Prompt
```yaml
{json.dumps(current_prompt, indent=2)}
```

## Successful Execution Patterns
{trace_analysis['successful_traces']} successful traces showed these patterns:
{json.dumps(trace_analysis['successful_patterns'], indent=2)}

## Failed Execution Patterns
{trace_analysis['failed_traces']} failed traces showed these patterns:
{json.dumps(trace_analysis['failed_patterns'], indent=2)}

## Ground Truth Analysis (NEW)
{self._format_ground_truth_analysis(trace_analysis.get('ground_truth_analysis', {}), agent_type, trace_analysis.get('_traces', []))}

## Problem-Specific Failure Analysis
{self._format_problem_specific_analysis(trace_analysis.get('problem_specific_analysis', {}), agent_type)}

## Existing Learned Insights (CRITICAL - DO NOT DUPLICATE)
{self._format_existing_insights(existing_insights or [])}

## Your Task
**CRITICAL: You must ADD new insights, NOT replace the original prompt content.**

**CRITICAL: AVOID DUPLICATES**
- Review ALL existing insights above carefully
- DO NOT generate insights that are semantically similar or duplicate of existing ones
- Only generate NEW, UNIQUE insights that add value beyond what already exists
- If an insight conveys the same meaning/recommendation as an existing one, DO NOT include it
- Focus on generating insights that address gaps or new failure patterns not covered by existing insights

Analyze the current prompt and performance data, then provide NEW INSIGHTS to ADD (not replace) that:
1. **Improves success rate** - Learn from successful patterns and add recommendations
2. **Addresses ground truth requirements** - Add guidance to ensure ALL expected services/requirements are identified
3. **Reduces latency** - Add efficient workflow suggestions (but don't remove original educational content)
4. **Reduces number of attempts** - Add guidance to use fewer, more effective tool calls
5. **Preserves original content** - DO NOT remove or modify original prompt sections. Only ADD new insights.
6. **Adds targeted guidance** - Include specific recommendations based on ground truth analysis and successful patterns

**IMPORTANT CONSTRAINTS:**
- DO NOT remove original Kubernetes resource reference sections
- DO NOT remove original workflow examples
- DO NOT remove original tool usage instructions
- ONLY ADD new insights in a "## Learned Insights" section
- Keep all original educational content intact

## Response Format
Provide NEW INSIGHTS to ADD (not the full prompt) in the following JSON format:
```json
{{
  "new_insights": [
    {{
      "type": "recommendation",
      "content": "## New Insight Title\\nSpecific guidance text here...",
      "reasoning": "Why this insight helps based on successful patterns"
    }},
    {{
      "type": "warning",
      "content": "## Avoid This Pattern\\nWarning text here...",
      "reasoning": "Why this pattern should be avoided based on failures"
    }}
  ],
  "changes_made": [
    "Description of new insight 1 and why",
    "Description of new insight 2 and why"
  ],
  "expected_improvements": {{
    "success_rate": "Expected improvement",
    "latency": "Expected improvement", 
    "attempts": "Expected improvement"
  }}
}}
```

**Remember**: You are ONLY providing NEW insights to ADD. Do NOT return the full prompt. The original prompt will be preserved automatically."""

        return prompt

    def _get_agent_description(self, agent_type: AgentType) -> str:
        """Get description of agent's role"""
        descriptions = {
            AgentType.DIAGNOSIS: "diagnosing faults in microservices applications",
            AgentType.LOCALIZATION: "localizing faults to specific components/services",
            AgentType.MITIGATION: "mitigating and fixing identified faults",
            AgentType.ROLLBACK: "rolling back changes when mitigation fails",
        }
        return descriptions.get(agent_type, "performing SRE tasks")

    def _sanitize_json_string(self, json_str: str) -> str:
        """Sanitize JSON string by escaping control characters"""
        import re

        # Replace unescaped control characters with escaped versions
        # This handles newlines, tabs, carriage returns, etc.
        # We need to be careful not to double-escape already escaped characters
        # First, replace literal newlines (not preceded by backslash) with \n
        json_str = re.sub(r"(?<!\\)\n", r"\\n", json_str)
        # Replace literal tabs (not preceded by backslash) with \t
        json_str = re.sub(r"(?<!\\)\t", r"\\t", json_str)
        # Replace literal carriage returns (not preceded by backslash) with \r
        json_str = re.sub(r"(?<!\\)\r", r"\\r", json_str)
        # Replace literal backslashes (not part of escape sequence) - be careful here
        # We'll handle backslashes more carefully

        return json_str

    def _parse_llm_response(self, response: Any, current_prompt: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Parse LLM response and extract optimized prompt

        Returns:
            Tuple of (parsed prompt, success: bool)
        """
        try:
            # Extract text from response
            if hasattr(response, "content"):
                response_text = response.content
            elif isinstance(response, str):
                response_text = response
            else:
                response_text = str(response)

            # Try to extract JSON from response
            # Look for JSON code block
            import re

            json_match = re.search(r"```json\s*(\{.*?\})\s*```", response_text, re.DOTALL)
            if not json_match:
                # Try without code block markers - look for new_insights format
                json_match = re.search(r'\{.*"new_insights".*\}', response_text, re.DOTALL)

            if json_match:
                json_str = json_match.group(1) if json_match.groups() else json_match.group(0)

                # Try parsing directly first
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError as e:
                    # If parsing fails, try sanitizing control characters
                    logger.debug(f"Initial JSON parse failed: {e}, attempting to sanitize...")
                    try:
                        sanitized_json = self._sanitize_json_string(json_str)
                        result = json.loads(sanitized_json)
                        logger.info("Successfully parsed JSON after sanitization")
                    except json.JSONDecodeError as e2:
                        logger.error(f"JSON parsing failed even after sanitization: {e2}")
                        logger.debug(f"JSON string (first 500 chars): {json_str[:500]}")
                        return {}, False

                # Check for new_insights format (additive learning)
                if "new_insights" in result:
                    # Log changes
                    if "changes_made" in result:
                        logger.info(f"LLM suggested new insights: {result['changes_made']}")
                    if "expected_improvements" in result:
                        logger.info(f"Expected improvements: {result['expected_improvements']}")

                    return result, True  # Return full result with new_insights
                
                # Fallback: check for old optimized_prompt format (for backward compatibility)
                elif "optimized_prompt" in result:
                    logger.warning("LLM returned old format (optimized_prompt). Converting to new_insights format...")
                    # Extract just the new parts from optimized_prompt (not full replacement)
                    optimized = result["optimized_prompt"]
                    # This is a simplified conversion - in practice, we'd need to diff
                    # For now, return empty to signal we need new_insights format
                    return {}, False

            # If JSON parsing fails, try to extract just the YAML structure
            logger.warning("Could not parse LLM response as JSON. Attempting to extract prompt sections...")

            # Fallback: return current prompt with failure flag
            return current_prompt, False

        except Exception as e:
            logger.error(f"Error parsing LLM response: {e}")
            logger.debug(f"Response was: {response_text[:500] if 'response_text' in locals() else 'N/A'}")
            return current_prompt, False


class LLMConfigOptimizer:
    """Uses LLM to optimize agent configuration (max_step, etc.) based on traces"""

    def __init__(self, model_name: str = "gemini/gemini-2.5-flash"):
        """Initialize LLM config optimizer"""
        self.model_name = model_name
        self.llm_backend = None
        self._initialize_llm()

    def _initialize_llm(self):
        """Initialize LLM backend"""
        try:
            import os

            from dotenv import load_dotenv

            # Load .env file to get API keys
            load_dotenv()

            from clients.stratus.llm_backend.get_llm_backend import LiteLLMBackend

            api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

            self.llm_backend = LiteLLMBackend(
                provider="litellm",
                model_name=self.model_name,
                url="",
                api_key=api_key or "",
                api_version="",
                seed=42,
                top_p=0.95,
                temperature=0.5,  # Lower temperature for config optimization (more deterministic)
                reasoning_effort="",
                thinking_tools="",
                thinking_budget_tools=0,
                max_tokens=4000,
            )
            logger.info(f"Initialized LLM config optimizer with model: {self.model_name}")
        except Exception as e:
            logger.error(f"Failed to initialize LLM config optimizer: {e}")
            self.llm_backend = None

    def optimize_config(
        self,
        agent_type: AgentType,
        current_config: Dict[str, Any],
        traces: List[AgentTrace],
        reward_spec: Optional[RewardSpec] = None,
    ) -> Dict[str, Any]:
        """
        Optimize agent configuration using LLM based on traces

        Args:
            agent_type: Type of agent
            current_config: Current configuration (YAML structure)
            traces: List of execution traces
            reward_spec: Reward specification

        Returns:
            Optimized configuration
        """
        if not self.llm_backend:
            logger.error("LLM backend not initialized. Cannot optimize config.")
            return current_config

        reward_spec = reward_spec or RewardSpec()

        # Analyze config usage patterns
        config_analysis = self._analyze_config_usage(traces, current_config)

        # Build optimization prompt
        optimization_prompt = self._build_config_optimization_prompt(
            agent_type, current_config, config_analysis, reward_spec
        )

        try:
            logger.info(f"Requesting LLM config optimization for {agent_type.value} agent...")
            response = self.llm_backend.inference(messages=[optimization_prompt], system_prompt=None)

            # Parse response
            optimized_config = self._parse_config_response(response, current_config)

            logger.info(f"Successfully optimized config for {agent_type.value} agent")
            return optimized_config

        except Exception as e:
            logger.error(f"Error during LLM config optimization: {e}")
            return current_config

    def _analyze_config_usage(self, traces: List[AgentTrace], current_config: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze how current config values are being used"""
        analysis = {
            "max_step_usage": [],
            "tool_call_counts": [],
            "success_with_current_limits": 0,
            "failures_due_to_limits": 0,
            "avg_steps_taken": 0.0,
        }

        max_step = current_config.get("max_step", 20)

        for trace in traces:
            tool_count = len(trace.tool_calls) if trace.tool_calls else 0
            analysis["tool_call_counts"].append(tool_count)

            # Check if agent hit step limit
            if tool_count >= max_step:
                if not trace.success:
                    analysis["failures_due_to_limits"] += 1
            else:
                if trace.success:
                    analysis["success_with_current_limits"] += 1

        if analysis["tool_call_counts"]:
            analysis["avg_steps_taken"] = sum(analysis["tool_call_counts"]) / len(analysis["tool_call_counts"])
            analysis["max_steps_taken"] = max(analysis["tool_call_counts"])
            analysis["min_steps_taken"] = min(analysis["tool_call_counts"])

        return analysis

    def _build_config_optimization_prompt(
        self,
        agent_type: AgentType,
        current_config: Dict[str, Any],
        config_analysis: Dict[str, Any],
        reward_spec: RewardSpec,
    ) -> str:
        """Build prompt for config optimization"""

        prompt = f"""You are optimizing the configuration for a {agent_type.value} agent in a Kubernetes SRE system.

## Current Configuration
```yaml
{json.dumps(current_config, indent=2)}
```

## Configuration Usage Analysis
- Average steps taken: {config_analysis['avg_steps_taken']:.1f}
- Maximum steps taken: {config_analysis.get('max_steps_taken', 0)}
- Minimum steps taken: {config_analysis.get('min_steps_taken', 0)}
- Successful runs with current limits: {config_analysis['success_with_current_limits']}
- Failures potentially due to step limits: {config_analysis['failures_due_to_limits']}

## Optimization Goals
Based on the reward specification:
- Success weight: {reward_spec.success_weight} (maximize)
- Latency weight: {reward_spec.latency_weight} (minimize, negative weight)
- Attempts weight: {reward_spec.attempts_weight} (minimize, negative weight)

## Your Task
Optimize the configuration to:
1. **Allow sufficient steps for success** - If many failures are due to step limits, increase max_step
2. **Avoid unnecessary steps** - If agents consistently finish in fewer steps, you could reduce max_step
3. **Balance efficiency and completeness** - Find the sweet spot

## Response Format
Provide optimized configuration in JSON:
```json
{{
  "optimized_config": {{
    "max_step": 20,
    "max_retry_attempts": 10,
    ...
  }},
  "changes_made": [
    "Description of change 1 and why",
    "Description of change 2 and why"
  ],
  "reasoning": "Explanation of optimization strategy"
}}
```

Focus on data-driven recommendations based on actual usage patterns."""

        return prompt

    def _parse_config_response(self, response: Any, current_config: Dict[str, Any]) -> Dict[str, Any]:
        """Parse LLM response for config optimization"""
        try:
            if hasattr(response, "content"):
                response_text = response.content
            elif isinstance(response, str):
                response_text = response
            else:
                response_text = str(response)

            import re

            json_match = re.search(r"```json\s*(\{.*?\})\s*```", response_text, re.DOTALL)
            if not json_match:
                json_match = re.search(r'\{.*"optimized_config".*\}', response_text, re.DOTALL)

            if json_match:
                json_str = json_match.group(1) if json_match.groups() else json_match.group(0)
                result = json.loads(json_str)

                if "optimized_config" in result:
                    optimized = result["optimized_config"]

                    if "changes_made" in result:
                        logger.info(f"Config changes: {result['changes_made']}")

                    # Merge with current config to preserve any fields not mentioned
                    merged = {**current_config, **optimized}
                    return merged

            return current_config

        except Exception as e:
            logger.error(f"Error parsing config optimization response: {e}")
            return current_config
