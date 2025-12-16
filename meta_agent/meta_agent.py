"""
Meta-Agent Orchestrator

Coordinates the iterative learning process:
1. Collects traces from agent executions
2. Analyzes patterns and generates insights
3. Updates agent guidelines based on learnings
4. Manages version control and rollback capabilities
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .guideline_generator import GuidelineGenerator, GuidelineUpdate
from .pattern_analyzer import Pattern, PatternAnalyzer, PatternType
from .trace_collector import AgentTrace, AgentType, ProblemContext, TraceCollector

logger = logging.getLogger(__name__)


@dataclass
class MetaAgentConfig:
    """Configuration for the meta-agent system"""

    learning_interval: int = 3600  # seconds between learning cycles
    min_traces_for_analysis: int = 10  # minimum traces needed for analysis
    confidence_threshold: float = 0.7  # minimum confidence for applying patterns
    enable_auto_updates: bool = True  # automatically apply guideline updates
    backup_original_prompts: bool = True  # backup original prompts before updates
    use_point_based_prompts: bool = True  # use point-based prompt system with LLM conflict detection
    points_storage_path: Optional[str] = None  # Run-specific path for storing points (None = use default)


class MetaAgent:
    """Main meta-agent orchestrator for iterative agent improvement"""

    def __init__(self, config: Optional[MetaAgentConfig] = None):
        self.config = config or MetaAgentConfig()

        # Initialize components
        self.trace_collector = TraceCollector()
        self.pattern_analyzer = PatternAnalyzer()
        self.guideline_generator = GuidelineGenerator(
            use_point_based=self.config.use_point_based_prompts,
            points_storage_path=self.config.points_storage_path
        )

        # Learning state
        self.last_learning_time = 0
        self.learning_cycles = 0
        self.total_patterns_learned = 0

        # Performance tracking
        self.performance_history: List[Dict[str, Any]] = []

        logger.info("Meta-Agent initialized")

    def start_learning_cycle(self) -> Dict[str, Any]:
        """Start a new learning cycle"""
        logger.info("Starting learning cycle")

        # Check if we have enough traces (only from current test run)
        all_traces = self.trace_collector.load_traces(include_historical=False)
        if len(all_traces) < self.config.min_traces_for_analysis:
            logger.warning(
                f"Not enough traces for analysis. Need {self.config.min_traces_for_analysis}, have {len(all_traces)}"
            )
            return {"status": "insufficient_traces", "traces_count": len(all_traces)}

        # Analyze patterns
        patterns = self.pattern_analyzer.analyze_traces(all_traces)
        logger.info(f"Identified {len(patterns)} patterns")

        # Filter patterns by confidence
        high_confidence_patterns = [p for p in patterns if p.confidence >= self.config.confidence_threshold]
        logger.info(f"Applying {len(high_confidence_patterns)} high-confidence patterns")

        # Generate guideline updates
        updates = self.guideline_generator.generate_guidelines(high_confidence_patterns)
        logger.info(f"Generated {len(updates)} guideline updates")

        # Apply updates if auto-updates are enabled
        applied_updates = []
        if self.config.enable_auto_updates:
            for update in updates:
                if self._apply_guideline_update(update):
                    applied_updates.append(update)

        # Update learning state
        self.last_learning_time = time.time()
        self.learning_cycles += 1
        self.total_patterns_learned += len(high_confidence_patterns)

        # Record performance
        performance_data = {
            "cycle": self.learning_cycles,
            "timestamp": datetime.now().isoformat(),
            "traces_analyzed": len(all_traces),
            "patterns_identified": len(patterns),
            "patterns_applied": len(high_confidence_patterns),
            "updates_generated": len(updates),
            "updates_applied": len(applied_updates),
        }
        self.performance_history.append(performance_data)

        logger.info(f"Learning cycle {self.learning_cycles} completed")

        return {
            "status": "success",
            "cycle": self.learning_cycles,
            "traces_analyzed": len(all_traces),
            "patterns_identified": len(patterns),
            "patterns_applied": len(high_confidence_patterns),
            "updates_generated": len(updates),
            "updates_applied": len(applied_updates),
            "applied_updates": [update.agent_type.value for update in applied_updates],
        }

    def _apply_guideline_update(self, update: GuidelineUpdate) -> bool:
        """Apply a guideline update"""
        try:
            # The guideline generator already applies changes when generating updates
            # This method can be used for additional validation or rollback logic
            logger.info(f"Applied guideline update for {update.agent_type.value} v{update.version}")
            return True
        except Exception as e:
            logger.error(f"Failed to apply guideline update: {e}")
            return False

    def collect_agent_trace(self, trace_id: str, agent_type: AgentType, problem_context: ProblemContext) -> AgentTrace:
        """Start collecting a trace for an agent execution"""
        return self.trace_collector.start_trace(trace_id, agent_type, problem_context)

    def add_tool_call(
        self, trace_id: str, tool_name: str, arguments: Dict[str, Any], success: bool, response: str, duration: float
    ) -> None:
        """Add a tool call to the current trace"""
        self.trace_collector.add_tool_call(trace_id, tool_name, arguments, success, response, duration)

    def add_thinking_step(self, trace_id: str, reasoning: str, tool_choice: str, justification: str) -> None:
        """Add a thinking step to the current trace"""
        self.trace_collector.add_thinking_step(trace_id, reasoning, tool_choice, justification)

    def end_agent_trace(
        self,
        trace_id: str,
        success: bool,
        final_submission: Optional[str] = None,
        ground_truth: Optional[Dict[str, Any]] = None,
        oracle_results: Optional[Dict[str, Any]] = None,
    ) -> AgentTrace:
        """End an agent trace
        
        Args:
            trace_id: Trace ID
            success: Whether the trace was successful
            final_submission: Final submission content
            ground_truth: Ground truth expectations for this problem
            oracle_results: Oracle evaluation results from conductor
        """
        return self.trace_collector.end_trace(trace_id, success, final_submission, ground_truth, oracle_results)

    def get_learning_status(self) -> Dict[str, Any]:
        """Get current learning status"""
        all_traces = self.trace_collector.load_traces(include_historical=False)
        trace_stats = self.trace_collector.get_trace_statistics()

        return {
            "learning_cycles": self.learning_cycles,
            "total_patterns_learned": self.total_patterns_learned,
            "last_learning_time": self.last_learning_time,
            "time_since_last_learning": time.time() - self.last_learning_time if self.last_learning_time > 0 else None,
            "total_traces": len(all_traces),
            "trace_statistics": trace_stats,
            "ready_for_learning": len(all_traces) >= self.config.min_traces_for_analysis,
        }

    def get_pattern_summary(self) -> Dict[str, Any]:
        """Get summary of learned patterns"""
        all_traces = self.trace_collector.load_traces(include_historical=False)
        patterns = self.pattern_analyzer.analyze_traces(all_traces)

        patterns_by_type = {}
        for pattern_type in PatternType:
            type_patterns = [p for p in patterns if p.pattern_type == pattern_type]
            patterns_by_type[pattern_type.value] = {
                "count": len(type_patterns),
                "high_confidence": len([p for p in type_patterns if p.confidence >= self.config.confidence_threshold]),
            }

        return {
            "total_patterns": len(patterns),
            "patterns_by_type": patterns_by_type,
            "high_confidence_patterns": len([p for p in patterns if p.confidence >= self.config.confidence_threshold]),
        }

    def get_guideline_history(self, agent_type: Optional[AgentType] = None) -> List[Dict[str, Any]]:
        """Get history of guideline updates"""
        updates = self.guideline_generator.get_guideline_history(agent_type)
        return [
            {
                "agent_type": update.agent_type.value,
                "version": update.version,
                "timestamp": update.timestamp,
                "patterns_applied": update.patterns_applied,
                "changes_count": len(update.changes),
            }
            for update in updates
        ]

    def rollback_agent(self, agent_type: AgentType, version: str) -> bool:
        """Rollback an agent to a specific version"""
        return self.guideline_generator.rollback_to_version(agent_type, version)

    def save_state(self, filepath: str) -> None:
        """Save meta-agent state to file"""
        state = {
            "config": {
                "learning_interval": self.config.learning_interval,
                "min_traces_for_analysis": self.config.min_traces_for_analysis,
                "confidence_threshold": self.config.confidence_threshold,
                "enable_auto_updates": self.config.enable_auto_updates,
                "backup_original_prompts": self.config.backup_original_prompts,
            },
            "learning_state": {
                "last_learning_time": self.last_learning_time,
                "learning_cycles": self.learning_cycles,
                "total_patterns_learned": self.total_patterns_learned,
            },
            "performance_history": self.performance_history,
        }

        import json

        with open(filepath, "w") as f:
            json.dump(state, f, indent=2, default=str)

        logger.info(f"Saved meta-agent state to {filepath}")

    def load_state(self, filepath: str) -> bool:
        """Load meta-agent state from file"""
        try:
            import json

            with open(filepath, "r") as f:
                state = json.load(f)

            # Update config
            if "config" in state:
                for key, value in state["config"].items():
                    if hasattr(self.config, key):
                        setattr(self.config, key, value)

            # Update learning state
            if "learning_state" in state:
                self.last_learning_time = state["learning_state"].get("last_learning_time", 0)
                self.learning_cycles = state["learning_state"].get("learning_cycles", 0)
                self.total_patterns_learned = state["learning_state"].get("total_patterns_learned", 0)

            # Load performance history
            if "performance_history" in state:
                self.performance_history = state["performance_history"]

            logger.info(f"Loaded meta-agent state from {filepath}")
            return True

        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return False

    def should_start_learning_cycle(self) -> bool:
        """Check if it's time to start a new learning cycle"""
        if not self.config.enable_auto_updates:
            return False

        time_since_last = time.time() - self.last_learning_time
        return time_since_last >= self.config.learning_interval

    def run_continuous_learning(self) -> None:
        """Run continuous learning in a loop"""
        logger.info("Starting continuous learning mode")

        while True:
            try:
                if self.should_start_learning_cycle():
                    result = self.start_learning_cycle()
                    logger.info(f"Learning cycle result: {result}")

                # Sleep for a short interval before checking again
                time.sleep(60)  # Check every minute

            except KeyboardInterrupt:
                logger.info("Stopping continuous learning")
                break
            except Exception as e:
                logger.error(f"Error in continuous learning: {e}")
                time.sleep(60)  # Wait before retrying

    def clean_all_guidelines(self) -> bool:
        """Clean all learned guidelines from agent prompts, resetting to original templates"""
        logger.info("🧹 Meta-Agent: Cleaning all learned guidelines...")

        try:
            # Use the guideline generator to clean all guidelines
            success = self.guideline_generator.clean_all_guidelines()

            if success:
                # Reset learning state
                self.last_learning_time = 0
                self.learning_cycles = 0
                self.total_patterns_learned = 0
                self.performance_history.clear()

                logger.info("🎉 Meta-Agent: Successfully cleaned all guidelines and reset learning state!")
                return True
            else:
                logger.error("❌ Meta-Agent: Failed to clean guidelines")
                return False

        except Exception as e:
            logger.error(f"❌ Meta-Agent: Error cleaning guidelines: {e}")
            return False
