"""
LLM-Enhanced Meta-Agent

Extends the base MetaAgent to use LLM-based optimization for prompts and configs
based on execution traces and reward specifications.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .llm_optimizer import LLMConfigOptimizer, LLMPromptOptimizer, RewardSpec
from .meta_agent import MetaAgent, MetaAgentConfig
from .trace_collector import AgentTrace, AgentType, TraceCollector

logger = logging.getLogger(__name__)


@dataclass
class LLMMetaAgentConfig(MetaAgentConfig):
    """Configuration for LLM-enhanced meta-agent"""

    # LLM settings
    llm_model: str = "gemini/gemini-2.5-flash"
    use_llm_optimization: bool = True

    # Reward specification
    reward_spec: Optional[RewardSpec] = None

    # Optimization settings
    optimize_prompts: bool = True
    optimize_configs: bool = True
    min_traces_for_llm_optimization: int = 5  # Minimum traces needed for LLM optimization


class LLMMetaAgent(MetaAgent):
    """Meta-Agent with LLM-based prompt and config optimization"""

    def __init__(self, config: Optional[LLMMetaAgentConfig] = None):
        """Initialize LLM-enhanced meta-agent"""
        # Initialize base meta-agent
        super().__init__(config)

        self.llm_config = config or LLMMetaAgentConfig()

        # Initialize LLM optimizers
        if self.llm_config.use_llm_optimization:
            self.prompt_optimizer = LLMPromptOptimizer(
                model_name=self.llm_config.llm_model, reward_spec=self.llm_config.reward_spec or RewardSpec()
            )
            self.config_optimizer = LLMConfigOptimizer(model_name=self.llm_config.llm_model)
        else:
            self.prompt_optimizer = None
            self.config_optimizer = None

        logger.info("LLM-Enhanced Meta-Agent initialized")

    def start_learning_cycle(self) -> Dict[str, Any]:
        """Start a learning cycle with LLM-based optimization"""
        logger.info("Starting LLM-enhanced learning cycle")

        # Get traces
        all_traces = self.trace_collector.load_traces(include_historical=False)

        if len(all_traces) < self.config.min_traces_for_analysis:
            logger.warning(
                f"Not enough traces for analysis. Need {self.config.min_traces_for_analysis}, have {len(all_traces)}"
            )
            return {"status": "insufficient_traces", "traces_count": len(all_traces)}

        # Run base pattern analysis
        patterns = self.pattern_analyzer.analyze_traces(all_traces)
        logger.info(f"Identified {len(patterns)} patterns from base analyzer")

        # Group traces by agent type
        traces_by_agent = self._group_traces_by_agent(all_traces)

        # Store current prompts and versions before any updates (for fallback)
        prompts_before_updates = {}
        versions_before_updates = {}
        for agent_type in [AgentType.DIAGNOSIS, AgentType.LOCALIZATION, AgentType.MITIGATION, AgentType.ROLLBACK]:
            prompts_before_updates[agent_type] = self.guideline_generator.prompt_templates.get(agent_type, {}).copy()
            versions_before_updates[agent_type] = self.guideline_generator.get_latest_version(agent_type)

        # Apply pattern-based updates FIRST (so LLM can optimize on top of them)
        base_updates = self.guideline_generator.generate_guidelines(patterns)
        applied_updates = []

        # Apply pattern-based updates
        pattern_based_applied = {}
        for update in base_updates:
            if self._apply_guideline_update(update):
                pattern_based_applied[update.agent_type] = True
                applied_updates.append(
                    {"agent_type": update.agent_type.value, "type": "pattern_based", "version": update.version}
                )
            else:
                pattern_based_applied[update.agent_type] = False

        # LLM-based optimization (on top of pattern-based updates)
        llm_updates = {}
        if self.llm_config.use_llm_optimization and len(all_traces) >= self.llm_config.min_traces_for_llm_optimization:
            llm_updates = self._optimize_with_llm(traces_by_agent)

        # Apply LLM optimizations or fallback to previous version
        # Only process agents that were attempted for LLM optimization
        for agent_type in llm_updates.get("prompts", {}).keys():
            llm_result = llm_updates["prompts"][agent_type]

            if llm_result is not None:
                # LLM optimization succeeded
                if self._apply_llm_prompt_update(agent_type, llm_result):
                    applied_updates.append(
                        {"agent_type": agent_type.value, "type": "llm_prompt_optimization", "source": "llm"}
                    )
            else:
                # LLM optimization failed - use previous prompt version as fallback
                logger.warning(
                    f"LLM optimization failed for {agent_type.value}, using previous prompt version as fallback"
                )

                # Try to load the version that existed before pattern-based updates
                previous_version = versions_before_updates.get(agent_type)
                previous_prompt = None

                if previous_version:
                    # Load the specific version from before pattern-based updates
                    previous_prompt = self.guideline_generator.load_version(agent_type, previous_version)

                if previous_prompt:
                    # Restore previous version
                    self.guideline_generator.prompt_templates[agent_type] = previous_prompt
                    # Save it as current (increment version)
                    restored_version = previous_version
                    # Generate new version number for the restored prompt
                    new_version = self.guideline_generator._generate_version_number(agent_type)
                    self.guideline_generator._save_updated_template(agent_type, new_version)
                    applied_updates.append(
                        {
                            "agent_type": agent_type.value,
                            "type": "previous_version_fallback",
                            "restored_version": restored_version,
                            "new_version": new_version,
                            "reason": "llm_optimization_failed",
                        }
                    )
                    logger.info(
                        f"Restored previous prompt version {restored_version} for {agent_type.value} (saved as v{new_version})"
                    )
                else:
                    # No previous version found, restore from prompts_before_updates or keep pattern-based
                    if prompts_before_updates.get(agent_type):
                        self.guideline_generator.prompt_templates[agent_type] = prompts_before_updates[agent_type]
                        # Save the restored prompt
                        new_version = self.guideline_generator._generate_version_number(agent_type)
                        self.guideline_generator._save_updated_template(agent_type, new_version)
                        applied_updates.append(
                            {
                                "agent_type": agent_type.value,
                                "type": "original_prompt_fallback",
                                "new_version": new_version,
                                "reason": "llm_optimization_failed_no_previous_version",
                            }
                        )
                        logger.info(f"Restored original prompt for {agent_type.value} (saved as v{new_version})")
                    elif pattern_based_applied.get(agent_type, False):
                        logger.info(
                            f"Keeping pattern-based update for {agent_type.value} (no previous version available)"
                        )
                    else:
                        logger.warning(f"No fallback available for {agent_type.value}, keeping current prompt")

        # For agents that didn't get LLM optimization (e.g., insufficient traces), keep pattern-based updates if applied
        for agent_type in [AgentType.DIAGNOSIS, AgentType.LOCALIZATION, AgentType.MITIGATION, AgentType.ROLLBACK]:
            if agent_type not in llm_updates.get("prompts", {}):
                if pattern_based_applied.get(agent_type, False):
                    logger.debug(f"Keeping pattern-based update for {agent_type.value} (no LLM optimization attempted)")

        # Update learning state
        self.last_learning_time = time.time()
        self.learning_cycles += 1

        return {
            "status": "success",
            "cycle": self.learning_cycles,
            "traces_analyzed": len(all_traces),
            "patterns_identified": len(patterns),
            "llm_optimizations": len(llm_updates.get("prompts", {})),
            "updates_applied": len(applied_updates),
            "applied_updates": applied_updates,
        }

    def _group_traces_by_agent(self, traces: List[AgentTrace]) -> Dict[AgentType, List[AgentTrace]]:
        """Group traces by agent type"""
        traces_by_agent = {}
        for agent_type in AgentType:
            agent_traces = [t for t in traces if t.agent_type == agent_type]
            if agent_traces:
                traces_by_agent[agent_type] = agent_traces
        return traces_by_agent

    def _optimize_with_llm(self, traces_by_agent: Dict[AgentType, List[AgentTrace]]) -> Dict[str, Any]:
        """Optimize prompts and configs using LLM"""
        updates = {"prompts": {}, "configs": {}}

        if not self.prompt_optimizer:
            return updates

        reward_spec = self.llm_config.reward_spec or RewardSpec()

        for agent_type, traces in traces_by_agent.items():
            if len(traces) < self.llm_config.min_traces_for_llm_optimization:
                logger.debug(f"Skipping LLM optimization for {agent_type.value}: insufficient traces ({len(traces)})")
                continue

            # Optimize prompt (now returns new_insights, not full prompt)
            if self.llm_config.optimize_prompts and self.prompt_optimizer:
                try:
                    current_prompt = self.guideline_generator.prompt_templates.get(agent_type, {})
                    if current_prompt:
                        # Get existing insights to pass to LLM for deduplication
                        existing_insights = self.guideline_generator.learned_insights.get(agent_type, [])
                        llm_response, success = self.prompt_optimizer.optimize_prompt(
                            agent_type=agent_type, 
                            current_prompt=current_prompt, 
                            traces=traces, 
                            reward_spec=reward_spec,
                            existing_insights=existing_insights
                        )
                        if success:
                            updates["prompts"][agent_type] = llm_response  # Contains new_insights dict
                            logger.info(f"LLM generated new insights for {agent_type.value}")
                        else:
                            logger.warning(f"LLM optimization failed for {agent_type.value}, will use fallback")
                            updates["prompts"][agent_type] = None  # Signal failure
                except Exception as e:
                    logger.error(f"Error optimizing prompt for {agent_type.value}: {e}")
                    updates["prompts"][agent_type] = None  # Signal failure

            # Optimize config
            if self.llm_config.optimize_configs and self.config_optimizer:
                try:
                    current_config = self._load_agent_config(agent_type)
                    if current_config:
                        optimized_config = self.config_optimizer.optimize_config(
                            agent_type=agent_type, current_config=current_config, traces=traces, reward_spec=reward_spec
                        )
                        updates["configs"][agent_type] = optimized_config
                        logger.info(f"LLM optimized config for {agent_type.value}")
                except Exception as e:
                    logger.error(f"Error optimizing config for {agent_type.value}: {e}")

        return updates

    def _load_agent_config(self, agent_type: AgentType) -> Optional[Dict[str, Any]]:
        """Load agent configuration file"""
        config_dir = Path(self.guideline_generator.config_dir)

        config_files = {
            AgentType.DIAGNOSIS: "diagnosis_agent_config.yaml",
            AgentType.LOCALIZATION: "localization_agent_config.yaml",
            AgentType.MITIGATION: "mitigation_agent_config.yaml",
            AgentType.ROLLBACK: "rollback_agent_config.yaml",
        }

        config_file = config_dir / config_files.get(agent_type)
        if config_file.exists():
            with open(config_file, "r") as f:
                return yaml.safe_load(f)
        return None

    def _apply_llm_prompt_update(self, agent_type: AgentType, llm_response: Dict[str, Any]) -> bool:
        """Apply LLM-optimized prompt update - ADDITIVE ONLY (preserves original)"""
        try:
            # Extract new insights from LLM response
            new_insights = llm_response.get("new_insights", [])
            
            if not new_insights:
                logger.warning(f"No new insights found in LLM response for {agent_type.value}")
                return False
            
            # Convert LLM insights to guideline changes format
            changes = []
            for insight in new_insights:
                insight_type = insight.get("type", "recommendation")
                content = insight.get("content", "")
                reasoning = insight.get("reasoning", "")
                
                # Map insight types to change types
                change_type_map = {
                    "recommendation": "add_recommendation",
                    "warning": "add_warning",
                    "guidance": "add_guidance",
                    "caution": "add_caution",
                }
                
                change_type = change_type_map.get(insight_type, "add_guidance")
                
                changes.append({
                    "type": change_type,
                    "content": content,
                    "pattern": reasoning,
                    "source": "llm_optimization",
                })
            
            # Apply changes using the additive method (preserves original)
            if changes:
                self.guideline_generator._apply_changes_to_template(agent_type, changes)
                
                # Generate version number
                version = self.guideline_generator._generate_version_number(agent_type)
                
                # Save updated template
                self.guideline_generator._save_updated_template(agent_type, version)
                
                logger.info(f"Applied {len(changes)} new insights for {agent_type.value} v{version} (original preserved)")
                return True
            else:
                logger.warning(f"No valid changes extracted from LLM response for {agent_type.value}")
                return False

        except Exception as e:
            logger.error(f"Failed to apply LLM prompt update: {e}")
            return False

    def optimize_agent_with_rewards(
        self, agent_type: AgentType, traces: List[AgentTrace], reward_spec: Optional[RewardSpec] = None
    ) -> Dict[str, Any]:
        """
        Optimize a specific agent based on traces and reward specification

        Args:
            agent_type: Agent type to optimize
            traces: Execution traces for this agent
            reward_spec: Reward specification (uses instance default if not provided)

        Returns:
            Dictionary with optimization results
        """
        if not self.prompt_optimizer:
            logger.error("LLM optimizer not initialized")
            return {"status": "error", "message": "LLM optimizer not initialized"}

        reward_spec = reward_spec or self.llm_config.reward_spec or RewardSpec()

        results = {"agent_type": agent_type.value, "traces_analyzed": len(traces), "status": "success"}

        # Optimize prompt (now returns new_insights, not full prompt)
        if self.llm_config.optimize_prompts:
            current_prompt = self.guideline_generator.prompt_templates.get(agent_type, {})
            if current_prompt:
                llm_response, success = self.prompt_optimizer.optimize_prompt(
                    agent_type=agent_type, current_prompt=current_prompt, traces=traces, reward_spec=reward_spec
                )

                if success and self._apply_llm_prompt_update(agent_type, llm_response):
                    results["prompt_optimized"] = True
                else:
                    results["prompt_optimized"] = False
                    results["status"] = "partial"
                    if not success:
                        results["error"] = "LLM optimization failed after retries"
            else:
                results["prompt_optimized"] = False
                results["error"] = "No current prompt found"

        # Optimize config
        if self.llm_config.optimize_configs and self.config_optimizer:
            current_config = self._load_agent_config(agent_type)
            if current_config:
                optimized_config = self.config_optimizer.optimize_config(
                    agent_type=agent_type, current_config=current_config, traces=traces, reward_spec=reward_spec
                )

                # Save optimized config
                config_dir = Path(self.guideline_generator.config_dir)
                config_files = {
                    AgentType.DIAGNOSIS: "diagnosis_agent_config.yaml",
                    AgentType.LOCALIZATION: "localization_agent_config.yaml",
                    AgentType.MITIGATION: "mitigation_agent_config.yaml",
                    AgentType.ROLLBACK: "rollback_agent_config.yaml",
                }

                config_file = config_dir / config_files.get(agent_type)
                if config_file.exists():
                    with open(config_file, "w") as f:
                        yaml.dump(optimized_config, f, default_flow_style=False)
                    results["config_optimized"] = True
                    logger.info(f"Saved optimized config for {agent_type.value}")
                else:
                    results["config_optimized"] = False
            else:
                results["config_optimized"] = False

        return results

    def set_reward_spec(self, reward_spec: RewardSpec):
        """Update reward specification"""
        self.llm_config.reward_spec = reward_spec
        if self.prompt_optimizer:
            self.prompt_optimizer.reward_spec = reward_spec
        logger.info(
            f"Updated reward specification: success={reward_spec.success_weight}, latency={reward_spec.latency_weight}, attempts={reward_spec.attempts_weight}"
        )
