"""
Guideline Generator for Meta-Agent System

Generates and updates agent guidelines based on learned patterns from traces.
Creates versioned prompt files and maintains a history of changes.
"""

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .pattern_analyzer import Pattern, PatternType
from .trace_collector import AgentType

# Import PromptPoint for type hints in point-based system
try:
    from .point_based_prompts import PromptPoint
except ImportError:
    PromptPoint = None  # Type: ignore

logger = logging.getLogger(__name__)


@dataclass
class GuidelineUpdate:
    agent_type: AgentType
    version: str
    timestamp: str
    changes: List[Dict[str, Any]]
    patterns_applied: List[str]
    performance_impact: Optional[Dict[str, Any]] = None


class GuidelineGenerator:
    """Generates and manages agent guidelines based on learned patterns"""

    def __init__(
        self,
        config_dir: str = "clients/stratus/configs",
        version_dir: str = "meta_agent/versions",
        use_point_based: bool = False,
        points_storage_path: Optional[str] = None,
    ):
        self.config_dir = Path(config_dir)
        self.version_dir = Path(version_dir)
        self.version_dir.mkdir(parents=True, exist_ok=True)

        # Load existing prompt templates
        self.prompt_templates = self._load_prompt_templates()
        self.guideline_history: List[GuidelineUpdate] = []

        # Store original prompts (preserved, never modified)
        self.original_prompts: Dict[AgentType, Dict[str, Any]] = {}
        # Store learned insights with verification status
        self.learned_insights: Dict[AgentType, List[Dict[str, Any]]] = {
            agent_type: [] for agent_type in AgentType
        }
        
        # Initialize original prompts if not already stored
        self._initialize_original_prompts()
        
        # Create backups of original prompts if they don't exist
        self._create_backup_prompts()
        
        # Point-based prompt system (optional)
        self.use_point_based = use_point_based
        self.point_manager = None
        if use_point_based:
            try:
                from .point_based_prompts import PointBasedPromptManager
                # Use run-specific storage path if provided, otherwise use default
                storage_path = points_storage_path or "meta_agent/point_prompts"
                # Skip loading if:
                # 1. Using run-specific path that doesn't exist yet (Round 1, folder just created but empty)
                # 2. Using default path during cleaning (we'll clear it anyway, so skip to avoid confusion)
                skip_load = (points_storage_path is not None and not Path(points_storage_path).exists()) or \
                           (points_storage_path is None)  # Skip during cleaning
                self.point_manager = PointBasedPromptManager(
                    storage_path=storage_path,
                    use_llm_detection=True,
                    use_llm_usage_detection=True,
                    skip_load=skip_load,
                    use_llm_primary=True  # Use LLM as primary method for point identification
                )
                logger.info(f"✅ Point-based prompt system enabled with LLM conflict detection and LLM usage detection")
                logger.info(f"   Points storage: {storage_path}")
                if skip_load:
                    if points_storage_path is None:
                        logger.info(f"   Skipping point load (cleaning phase)")
                    else:
                        logger.info(f"   Skipping point load (Round 1 - starting fresh)")
                # Initialize point-based system (does NOT parse original prompts)
                self._initialize_point_based_system()
            except Exception as e:
                logger.warning(f"Failed to initialize point-based system: {e}. Falling back to traditional system.")
                self.use_point_based = False

    def _load_prompt_templates(self) -> Dict[AgentType, Dict[str, Any]]:
        """Load existing prompt templates from YAML files"""
        templates = {}

        agent_files = {
            AgentType.DIAGNOSIS: "diagnosis_agent_prompts.yaml",
            AgentType.LOCALIZATION: "localization_agent_prompts.yaml",
            AgentType.MITIGATION: "mitigation_agent_prompts.yaml",
            AgentType.ROLLBACK: "rollback_agent_prompts.yaml",
        }

        for agent_type, filename in agent_files.items():
            filepath = self.config_dir / filename
            if filepath.exists():
                with open(filepath, "r") as f:
                    templates[agent_type] = yaml.safe_load(f)
            else:
                logger.warning(f"Prompt template not found: {filepath}")
                templates[agent_type] = {}

        return templates
    
    def _initialize_original_prompts(self) -> None:
        """Store original prompts if not already stored (preserve them forever)"""
        for agent_type in AgentType:
            if agent_type not in self.original_prompts:
                # Store a deep copy of the current template as original
                import copy
                if agent_type in self.prompt_templates:
                    self.original_prompts[agent_type] = copy.deepcopy(self.prompt_templates[agent_type])
                    logger.debug(f"Stored original prompt for {agent_type.value}")
                else:
                    logger.warning(f"No template found to store as original for {agent_type.value}")
    
    def _initialize_point_based_system(self) -> None:
        """Initialize point-based system
        
        NOTE: We do NOT parse original prompts into points.
        Original prompts are kept intact and only learned insights are stored as points.
        Points are loaded directly from JSON files in PointBasedPromptManager.__init__.
        Only learned points (source='learned') are loaded - original points are never stored.
        """
        if not self.point_manager:
            return
        
        logger.info("Initializing point-based prompt system...")
        logger.info("  Original prompts will be kept intact (not converted to points)")
        logger.info("  Only learned insights will be stored as points")
        
        total_learned = 0
        for agent_type in AgentType:
            learned_points = len([p for p in self.point_manager.points[agent_type] if p.source == "learned"])
            if learned_points > 0:
                logger.info(
                    f"  Loaded {learned_points} learned points for {agent_type.value} from {self.point_manager.storage_path}"
                )
                total_learned += learned_points
        
        if total_learned == 0:
            logger.info("  No learned points found - starting fresh (this is expected for Round 1)")
        
        logger.info("✅ Point-based system initialized (only learned insights are points)")

    def generate_guidelines(self, patterns: List[Pattern]) -> List[GuidelineUpdate]:
        """Generate guideline updates based on learned patterns"""
        updates = []

        # Group patterns by agent type
        patterns_by_agent = self._group_patterns_by_agent(patterns)

        for agent_type, agent_patterns in patterns_by_agent.items():
            if not agent_patterns:
                continue

            update = self._generate_agent_guidelines(agent_type, agent_patterns)
            if update:
                updates.append(update)
                self.guideline_history.append(update)

        return updates

    def _group_patterns_by_agent(self, patterns: List[Pattern]) -> Dict[AgentType, List[Pattern]]:
        """Group patterns by relevant agent types"""
        patterns_by_agent = defaultdict(list)

        for pattern in patterns:
            # Determine which agents this pattern applies to
            relevant_agents = self._get_relevant_agents(pattern)
            for agent_type in relevant_agents:
                patterns_by_agent[agent_type].append(pattern)

        return patterns_by_agent

    def _get_relevant_agents(self, pattern: Pattern) -> List[AgentType]:
        """Determine which agents a pattern is relevant for"""
        # For now, apply all patterns to all agents
        # In the future, this could be more sophisticated based on pattern content
        return list(AgentType)

    def _generate_agent_guidelines(self, agent_type: AgentType, patterns: List[Pattern]) -> Optional[GuidelineUpdate]:
        """Generate guidelines for a specific agent type"""
        if agent_type not in self.prompt_templates:
            logger.warning(f"No template found for agent type: {agent_type}")
            return None

        # Create new version
        version = self._generate_version_number(agent_type)
        timestamp = datetime.now().isoformat()

        # Generate guideline changes
        changes = []
        patterns_applied = []

        # Process different types of patterns
        # NOTE: SUCCESS_PATTERN and THINKING_PATTERN are skipped to avoid adding
        # "Effective Tool Sequences" and "Thinking Process Guidelines"
        for pattern in patterns:
            # Skip SUCCESS_PATTERN (Effective Tool Sequences)
            if pattern.pattern_type == PatternType.SUCCESS_PATTERN:
                logger.debug(f"Skipping SUCCESS_PATTERN for {agent_type}: {pattern.description}")
                continue

            # Skip THINKING_PATTERN (Thinking Process Guidelines)
            elif pattern.pattern_type == PatternType.THINKING_PATTERN:
                logger.debug(f"Skipping THINKING_PATTERN for {agent_type}: {pattern.description}")
                continue

            elif pattern.pattern_type == PatternType.FAILURE_PATTERN:
                change = self._apply_failure_pattern(agent_type, pattern)
                if change:
                    changes.append(change)
                    patterns_applied.append(pattern.description)

            elif pattern.pattern_type == PatternType.TOOL_EFFECTIVENESS:
                change = self._apply_tool_effectiveness_pattern(agent_type, pattern)
                if change:
                    changes.append(change)
                    patterns_applied.append(pattern.description)

        if not changes:
            return None

        # Create guideline update
        update = GuidelineUpdate(
            agent_type=agent_type,
            version=version,
            timestamp=timestamp,
            changes=changes,
            patterns_applied=patterns_applied,
        )

        # Apply changes to prompt template
        self._apply_changes_to_template(agent_type, changes)

        # Save updated template
        self._save_updated_template(agent_type, version)

        return update

    def _apply_success_pattern(self, agent_type: AgentType, pattern: Pattern) -> Optional[Dict[str, Any]]:
        """Apply a success pattern to agent guidelines"""
        # Extract tool sequence from pattern description
        if "Successful tool sequence:" in pattern.description:
            sequence = pattern.description.split("Successful tool sequence: ")[1]
            tools = [tool.strip() for tool in sequence.split(" -> ")]

            # Add guidance about effective tool sequences
            guidance = f"""
## Effective Tool Sequences
Based on successful past executions, the following tool sequence has proven effective:
{' -> '.join(tools)}

Consider using this sequence when facing similar problems. This pattern has a {pattern.confidence:.1%} success rate.
"""

            return {"type": "add_guidance", "section": "system", "content": guidance, "pattern": pattern.description}

        return None

    def _apply_failure_pattern(self, agent_type: AgentType, pattern: Pattern) -> Optional[Dict[str, Any]]:
        """Apply a failure pattern to agent guidelines"""
        if "Common failure point:" in pattern.description:
            tool_name = pattern.description.split("Common failure point: ")[1]

            # Add warning about problematic tool usage
            warning = f"""
## Tool Usage Warnings
⚠️ **{tool_name}** has been identified as a common failure point.
- Review parameters carefully before calling this tool
- Consider alternative approaches if this tool fails
- Add error handling and validation
"""

            return {"type": "add_warning", "section": "system", "content": warning, "pattern": pattern.description}

        return None

    def _apply_tool_effectiveness_pattern(self, agent_type: AgentType, pattern: Pattern) -> Optional[Dict[str, Any]]:
        """Apply tool effectiveness pattern to guidelines"""
        if "Highly effective tool:" in pattern.description:
            tool_name = pattern.description.split("Highly effective tool: ")[1]

            guidance = f"""
## Recommended Tools
✅ **{tool_name}** has shown high effectiveness in past executions.
- Success rate: {pattern.confidence:.1%}
- Consider prioritizing this tool when appropriate
"""

            return {
                "type": "add_recommendation",
                "section": "system",
                "content": guidance,
                "pattern": pattern.description,
            }

        elif "Problematic tool:" in pattern.description:
            tool_name = pattern.description.split("Problematic tool: ")[1]

            warning = f"""
## Tool Usage Caution
⚠️ **{tool_name}** has shown low effectiveness.
- Success rate: {1 - pattern.confidence:.1%}
- Use with caution and consider alternatives
- Add additional validation before calling this tool
"""

            return {"type": "add_caution", "section": "system", "content": warning, "pattern": pattern.description}

        return None

    def _apply_thinking_pattern(self, agent_type: AgentType, pattern: Pattern) -> Optional[Dict[str, Any]]:
        """Apply thinking pattern to guidelines"""
        if "Detailed reasoning improves success" in pattern.description:
            tool_choice = pattern.description.split("Detailed reasoning improves success for ")[1]

            guidance = f"""
## Thinking Process Guidelines
When choosing **{tool_choice}**, provide detailed reasoning:
- Explain your analysis step by step
- Consider multiple approaches before deciding
- Justify your tool choice with specific reasoning
- Aim for at least 20 words of analysis
"""

            return {
                "type": "add_thinking_guidance",
                "section": "thinking",
                "content": guidance,
                "pattern": pattern.description,
            }

        return None

    def _apply_changes_to_template(self, agent_type: AgentType, changes: List[Dict[str, Any]]) -> None:
        """Apply changes to the prompt template - ADDITIVE ONLY (preserves original)"""
        # Always start with original prompt
        import copy
        if agent_type not in self.original_prompts:
            self._initialize_original_prompts()
        
        # Restore from original
        self.prompt_templates[agent_type] = copy.deepcopy(self.original_prompts[agent_type])
        template = self.prompt_templates[agent_type]

        # Collect all guideline content for new insights
        new_insights = []

        for change in changes:
            if change["type"] in ["add_guidance", "add_warning", "add_recommendation", "add_caution"]:
                # Store as new insight with verification status
                insight = {
                    "type": change["type"],
                    "content": change["content"],
                    "pattern": change.get("pattern", ""),
                    "timestamp": datetime.now().isoformat(),
                    "verified": False,  # Not yet verified through interactions
                    "verification_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                }
                new_insights.append(insight)

            elif change["type"] == "add_thinking_guidance":
                insight = {
                    "type": "add_thinking_guidance",
                    "content": change["content"],
                    "pattern": change.get("pattern", ""),
                    "timestamp": datetime.now().isoformat(),
                    "verified": False,
                    "verification_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                }
                new_insights.append(insight)

        # LLM should have already avoided duplicates in the prompt, but keep simple check as backup
        # Only do quick check if there are many existing insights (LLM might have missed something)
        existing_insights = self.learned_insights.get(agent_type, [])
        deduplicated_insights = []
        if len(existing_insights) > 20:
            logger.debug(f"Many existing insights ({len(existing_insights)}), doing backup duplicate check")
            for new_insight in new_insights:
                new_content = new_insight.get("content", "")
                if new_content and not self._simple_duplicate_check(new_content, existing_insights + deduplicated_insights):
                    deduplicated_insights.append(new_insight)
                elif new_content:
                    logger.debug(f"Backup check: Skipping duplicate insight for {agent_type.value}: {new_content[:50]}...")
                else:
                    # Empty content, skip
                    logger.debug(f"Skipping insight with empty content")
        else:
            # Trust LLM's deduplication if we have few existing insights
            deduplicated_insights = new_insights
            logger.debug(f"Trusting LLM deduplication ({len(existing_insights)} existing insights)")

        # Add new insights to learned insights list (no limit - insights accumulate)
        self.learned_insights[agent_type].extend(deduplicated_insights)
        
        # If using point-based system, add insights as points and resolve conflicts
        if self.use_point_based and self.point_manager:
            # Track new point IDs for incremental conflict detection
            new_point_ids = []
            for insight in deduplicated_insights:
                point = self.point_manager.add_learned_insight(agent_type, insight)
                new_point_ids.append(point.id)
                # Store point_id in insight for validation mapping
                insight["point_id"] = point.id
                logger.debug(f"Added insight as point {point.id} for {agent_type.value}")
            
            # Incremental conflict detection: Only check conflicts involving new points
            # This is much more efficient than checking all pairs
            if new_point_ids:
                conflicts = self.point_manager.detect_conflicts(agent_type, new_point_ids=new_point_ids)
                if conflicts:
                    conflict_count = sum(1 for c in conflicts.values() if c)
                    logger.info(
                        f"Detected {conflict_count} conflicts for {agent_type.value} (incremental check: {len(new_point_ids)} new points), resolving..."
                    )
                else:
                    logger.debug(f"No conflicts detected for {agent_type.value} (checked {len(new_point_ids)} new points)")
            else:
                # No new points, skip conflict detection
                conflicts = {}
            
            # Resolve conflicts and rebuild from points
            active_points = self.point_manager.resolve_conflicts(agent_type)
            logger.info(f"Resolved conflicts: {len(active_points)} active points for {agent_type.value}")
            
            # Rebuild prompt from points
            self._rebuild_prompt_from_points(agent_type)
        else:
            # Traditional approach: Build prompt by combining original + insights
            self._rebuild_prompt_from_original_and_insights(agent_type)

    def _rebuild_prompt_from_original_and_insights(self, agent_type: AgentType) -> None:
        """Rebuild prompt from original + verified insights + new unverified insights"""
        import copy
        template = copy.deepcopy(self.original_prompts[agent_type])
        
        # Collect all insights (both verified and unverified)
        guideline_content = []
        thinking_content = []
        
        for insight in self.learned_insights[agent_type]:
            if insight["type"] == "add_thinking_guidance":
                thinking_content.append(insight["content"])
            else:
                # Add verification status marker
                verification_marker = "✅ VERIFIED" if insight["verified"] else "⚠️ UNVERIFIED (being tested)"
                guideline_content.append(f"\n{verification_marker}\n{insight['content']}")
        
        # Append learned insights to system prompt
        if guideline_content:
            insights_section = "\n\n## Learned Insights (Additive - Original Content Preserved Above)\n"
            insights_section += "The following insights have been learned from past executions. Original prompt content is preserved above.\n"
            insights_section += "".join(guideline_content)

            if "system" not in template:
                template["system"] = ""

            template["system"] += insights_section
        
        # Append thinking guidelines if any
        if thinking_content:
            if "thinking_guidelines" not in template:
                template["thinking_guidelines"] = ""
            template["thinking_guidelines"] += "".join(thinking_content)
        
        # Update the template
        self.prompt_templates[agent_type] = template
    
    def _rebuild_prompt_from_points(self, agent_type: AgentType) -> None:
        """Rebuild prompt from point-based system
        
        Keeps original prompt intact and only appends learned insights as a section.
        """
        if not self.point_manager:
            return
        
        # Start with original prompt (keep it intact)
        import copy
        template = copy.deepcopy(self.original_prompts.get(agent_type, {}))
        
        # Get only learned points (not original points)
        learned_points = [p for p in self.point_manager.points[agent_type] 
                         if p.source == "learned" and p.active]
        
        if not learned_points:
            # No learned insights, just use original prompt
            self.prompt_templates[agent_type] = template
            logger.info(f"Rebuilt {agent_type.value} prompt (no learned insights, using original only)")
            return
        
        # Resolve conflicts for learned points
        active_learned_points = self.point_manager.resolve_conflicts(agent_type)
        active_learned_points = [p for p in active_learned_points if p.source == "learned"]
        
        # Build learned insights section
        insights_section = "\n\n## Learned Insights (Additive - Original Content Preserved Above)\n"
        insights_section += "The following insights have been learned from past executions. Original prompt content is preserved above.\n\n"
        
        # Group learned points by category
        by_category: Dict[str, List[PromptPoint]] = {}
        for point in active_learned_points:
            if point.category not in by_category:
                by_category[point.category] = []
            by_category[point.category].append(point)
        
        # Sort points: verified first, then by priority
        for category in by_category:
            by_category[category].sort(
                key=lambda p: (not p.verified, -p.priority, -p.success_count)
            )
        
        # Build insights content
        insight_parts = []
        
        # Tool usage insights
        if "tool_usage" in by_category:
            insight_parts.append("### Tool Usage Guidelines\n")
            for point in by_category["tool_usage"]:
                marker = "✅ VERIFIED" if point.verified else "⚠️ UNVERIFIED (being tested)"
                insight_parts.append(f"{marker}\n{point.content}\n")
        
        # Workflow insights
        if "workflow" in by_category:
            insight_parts.append("\n### Workflow Guidelines\n")
            for point in by_category["workflow"]:
                marker = "✅ VERIFIED" if point.verified else "⚠️ UNVERIFIED (being tested)"
                insight_parts.append(f"{marker}\n{point.content}\n")
        
        # Warnings
        if "warning" in by_category:
            insight_parts.append("\n### Important Warnings\n")
            for point in by_category["warning"]:
                marker = "✅ VERIFIED" if point.verified else "⚠️ UNVERIFIED (being tested)"
                insight_parts.append(f"{marker}\n{point.content}\n")
        
        # General insights (catch-all)
        if "general" in by_category:
            for point in by_category["general"]:
                marker = "✅ VERIFIED" if point.verified else "⚠️ UNVERIFIED (being tested)"
                insight_parts.append(f"{marker}\n{point.content}\n")
        
        # Other categories
        for category in by_category:
            if category not in ["tool_usage", "workflow", "warning", "general"]:
                insight_parts.append(f"\n### {category.title()}\n")
                for point in by_category[category]:
                    marker = "✅ VERIFIED" if point.verified else "⚠️ UNVERIFIED (being tested)"
                    insight_parts.append(f"{marker}\n{point.content}\n")
        
        insights_section += "".join(insight_parts)
        
        # Append learned insights to original system prompt
        if "system" not in template:
            template["system"] = ""
        
        # Remove any existing "Learned Insights" section from original prompt (if present)
        system_prompt = template.get("system", "")
        if "## Learned Insights" in system_prompt:
            parts = system_prompt.split("## Learned Insights", 1)
            system_prompt = parts[0].rstrip()
            template["system"] = system_prompt
        
        # Append new learned insights section
        template["system"] += insights_section
        
        # Update the template
        self.prompt_templates[agent_type] = template
        logger.info(
            f"Rebuilt {agent_type.value} prompt: original prompt preserved, "
            f"{len(active_learned_points)} learned insights appended"
        )
    
    def _is_duplicate_insight(self, new_insight: Dict[str, Any], existing_insights: List[Dict[str, Any]]) -> bool:
        """Check if a new insight is a duplicate of an existing one using LLM"""
        if not existing_insights:
            return False
        
        new_content = new_insight.get("content", "").strip()
        
        # If no content, not a duplicate
        if not new_content:
            return False
        
        # Use LLM to check for semantic similarity
        try:
            llm_backend = self._get_llm_backend()
            if not llm_backend:
                # Fallback to simple check if LLM not available
                logger.warning("LLM backend not available for duplicate checking, using simple fallback")
                return self._simple_duplicate_check(new_content, existing_insights)
            
            # Check against existing insights (batch check for efficiency)
            for existing in existing_insights:
                existing_content = existing.get("content", "").strip()
                if not existing_content:
                    continue
                
                # Use LLM to determine if insights are semantically similar/duplicate
                is_duplicate = self._llm_check_duplicate(new_content, existing_content, llm_backend)
                if is_duplicate:
                    logger.debug(f"LLM identified duplicate insight for {new_insight.get('type', 'unknown')}")
                    return True
            
            return False
        except Exception as e:
            logger.warning(f"Error checking duplicate with LLM: {e}, using simple fallback")
            return self._simple_duplicate_check(new_content, existing_insights)
    
    def _get_llm_backend(self):
        """Get or initialize LLM backend for duplicate checking"""
        if not hasattr(self, '_llm_backend') or self._llm_backend is None:
            try:
                import os
                from dotenv import load_dotenv
                from clients.stratus.llm_backend.get_llm_backend import LiteLLMBackend
                
                load_dotenv()
                api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
                
                if api_key:
                    self._llm_backend = LiteLLMBackend(
                        provider="litellm",
                        model_name="gemini/gemini-2.5-flash",
                        url="",
                        api_key=api_key,
                        api_version="",
                        seed=42,
                        top_p=0.95,
                        temperature=0.0,  # Low temperature for deterministic duplicate checking
                        reasoning_effort="",
                        thinking_tools="",
                        thinking_budget_tools=0,
                        max_tokens=1000,
                    )
                    logger.debug("Initialized LLM backend for duplicate checking")
                else:
                    logger.warning("No API key found for LLM duplicate checking")
                    self._llm_backend = None
            except Exception as e:
                logger.warning(f"Failed to initialize LLM backend for duplicate checking: {e}")
                self._llm_backend = None
        
        return self._llm_backend
    
    def _llm_check_duplicate(self, new_content: str, existing_content: str, llm_backend) -> bool:
        """Use LLM to check if two insights are semantically duplicate"""
        prompt = f"""You are an expert at analyzing AI agent prompt guidelines. 

Compare these two insights and determine if they are semantically similar or duplicate (convey the same meaning/recommendation).

**Insight 1 (Existing):**
{existing_content}

**Insight 2 (New):**
{new_content}

Are these two insights semantically similar or duplicate? Do they convey the same meaning, recommendation, or warning?

Respond with ONLY a JSON object in this exact format:
{{
  "is_duplicate": true or false,
  "reasoning": "brief explanation"
}}

Be strict: only return true if they are essentially the same recommendation/guidance. Minor wording differences are OK if the meaning is the same."""

        try:
            response = llm_backend.inference(messages=[prompt], system_prompt=None)
            
            # Parse JSON response
            import json
            import re
            
            # Extract JSON from response
            json_match = re.search(r'\{[^{}]*"is_duplicate"[^{}]*\}', response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result.get("is_duplicate", False)
            else:
                # Fallback: check if response contains "true" or "duplicate"
                response_lower = response.lower()
                if "true" in response_lower or "duplicate" in response_lower:
                    return True
                return False
        except Exception as e:
            logger.warning(f"Error in LLM duplicate check: {e}")
            return False
    
    def _simple_duplicate_check(self, new_content: str, existing_insights: List[Dict[str, Any]]) -> bool:
        """Simple fallback duplicate check using word overlap"""
        import re
        new_content_normalized = re.sub(r'[✅⚠️🟢🔴]', '', new_content.lower())
        new_content_normalized = re.sub(r'verified|unverified|being tested', '', new_content_normalized, flags=re.IGNORECASE)
        new_content_normalized = re.sub(r'\s+', ' ', new_content_normalized).strip()
        new_words = set(new_content_normalized.split())
        
        if len(new_words) == 0:
            return False
        
        for existing in existing_insights:
            existing_content = existing.get("content", "").strip().lower()
            existing_content = re.sub(r'[✅⚠️🟢🔴]', '', existing_content)
            existing_content = re.sub(r'verified|unverified|being tested', '', existing_content, flags=re.IGNORECASE)
            existing_content = re.sub(r'\s+', ' ', existing_content).strip()
            existing_words = set(existing_content.split())
            
            if len(existing_words) == 0:
                continue
            
            overlap = len(new_words & existing_words)
            similarity = overlap / max(len(new_words), len(existing_words))
            
            if similarity > 0.8:
                return True
        
        return False

    def update_insight_verification(self, agent_type: AgentType, insight_index: int, success: bool) -> None:
        """Update verification status of an insight based on interaction results"""
        if agent_type not in self.learned_insights:
            return
        
        insights = self.learned_insights[agent_type]
        if 0 <= insight_index < len(insights):
            insight = insights[insight_index]
            insight["verification_count"] += 1
            if success:
                insight["success_count"] += 1
            else:
                insight["failure_count"] += 1
            
            # If using point-based system, update the corresponding point
            if self.use_point_based and self.point_manager:
                # Find the point that corresponds to this insight
                # (We can match by content or maintain a mapping)
                point_id = insight.get("point_id")
                if point_id:
                    self.point_manager.validate_point(agent_type, point_id, success)
                    # Rebuild from points after validation
                    self._rebuild_prompt_from_points(agent_type)
                    return
            
            # Traditional approach: Mark as verified after 3 successful uses, or remove if consistently failing
            if insight["verification_count"] >= 3:
                if insight["success_count"] >= 2:
                    insight["verified"] = True
                    logger.info(f"✅ Insight {insight_index} for {agent_type.value} marked as VERIFIED")
                    self._rebuild_prompt_from_original_and_insights(agent_type)
                elif insight["failure_count"] >= 2:
                    # Remove consistently failing insights
                    logger.info(f"❌ Removing consistently failing insight {insight_index} for {agent_type.value}")
                    insights.pop(insight_index)
                    self._rebuild_prompt_from_original_and_insights(agent_type)
            elif insight["failure_count"] >= 2:
                # Remove after 2 failures (aggressive removal)
                logger.info(f"❌ Removing failing insight {insight_index} for {agent_type.value} (2 failures)")
                insights.pop(insight_index)
                self._rebuild_prompt_from_original_and_insights(agent_type)
    
    def remove_bad_insight(self, agent_type: AgentType, insight_index: int) -> None:
        """Remove a specific bad insight (doesn't touch original)"""
        if agent_type not in self.learned_insights:
            return
        
        insights = self.learned_insights[agent_type]
        if 0 <= insight_index < len(insights):
            removed = insights.pop(insight_index)
            logger.info(f"Removed insight {insight_index} from {agent_type.value}: {removed.get('pattern', 'unknown')}")
            self._rebuild_prompt_from_original_and_insights(agent_type)

    def _save_updated_template(self, agent_type: AgentType, version: str) -> None:
        """Save the updated template with versioning"""
        # Ensure backup exists before saving updates
        self._create_backup_prompts()
        
        # Save to version directory
        version_file = self.version_dir / f"{agent_type.value}_v{version}.yaml"
        with open(version_file, "w") as f:
            yaml.dump(self.prompt_templates[agent_type], f, default_flow_style=False)

        # Save to main config directory (overwrite current)
        main_file = self.config_dir / f"{agent_type.value}_agent_prompts.yaml"
        with open(main_file, "w") as f:
            yaml.dump(self.prompt_templates[agent_type], f, default_flow_style=False)

        logger.info(f"Saved updated template for {agent_type.value} v{version}")

    def _generate_version_number(self, agent_type: AgentType) -> str:
        """Generate a new version number for the agent"""
        # Find existing versions for this agent
        version_files = list(self.version_dir.glob(f"{agent_type.value}_v*.yaml"))

        if not version_files:
            return "1.0.0"

        # Extract version numbers and increment
        versions = []
        for file in version_files:
            try:
                version_str = file.stem.split("_v")[1]
                versions.append(tuple(map(int, version_str.split("."))))
            except:
                continue

        if not versions:
            return "1.0.0"

        # Increment patch version
        latest_version = max(versions)
        new_version = (latest_version[0], latest_version[1], latest_version[2] + 1)
        return ".".join(map(str, new_version))

    def get_latest_version(self, agent_type: AgentType) -> Optional[str]:
        """Get the latest version number for an agent"""
        version_files = list(self.version_dir.glob(f"{agent_type.value}_v*.yaml"))

        if not version_files:
            return None

        # Extract version numbers
        versions = []
        for file in version_files:
            try:
                version_str = file.stem.split("_v")[1]
                versions.append((tuple(map(int, version_str.split("."))), version_str))
            except:
                continue

        if not versions:
            return None

        # Return the latest version string
        latest = max(versions, key=lambda x: x[0])
        return latest[1]

    def load_version(self, agent_type: AgentType, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Load a specific version of prompt template, or latest if version is None"""
        if version is None:
            version = self.get_latest_version(agent_type)
            if version is None:
                return None

        version_file = self.version_dir / f"{agent_type.value}_v{version}.yaml"

        if not version_file.exists():
            logger.warning(f"Version {version} not found for {agent_type.value}")
            return None

        try:
            with open(version_file, "r") as f:
                template = yaml.safe_load(f)
            logger.info(f"Loaded version {version} for {agent_type.value}")
            return template
        except Exception as e:
            logger.error(f"Error loading version {version} for {agent_type.value}: {e}")
            return None

    def get_guideline_history(self, agent_type: Optional[AgentType] = None) -> List[GuidelineUpdate]:
        """Get history of guideline updates"""
        if agent_type:
            return [update for update in self.guideline_history if update.agent_type == agent_type]
        return self.guideline_history

    def rollback_to_version(self, agent_type: AgentType, version: str) -> bool:
        """Rollback agent guidelines to a specific version"""
        version_file = self.version_dir / f"{agent_type.value}_v{version}.yaml"

        if not version_file.exists():
            logger.error(f"Version {version} not found for {agent_type.value}")
            return False

        # Load the version
        with open(version_file, "r") as f:
            template = yaml.safe_load(f)

        # Apply to current template
        self.prompt_templates[agent_type] = template

        # Save to main config
        main_file = self.config_dir / f"{agent_type.value}_agent_prompts.yaml"
        with open(main_file, "w") as f:
            yaml.dump(template, f, default_flow_style=False)

        logger.info(f"Rolled back {agent_type.value} to version {version}")
        return True

    def clean_all_guidelines(self) -> bool:
        """Clean all learned guidelines from agent prompts, resetting to backup templates"""
        logger.info("🧹 Cleaning all learned guidelines from agent prompts...")

        try:
            # Load original prompt templates from backups
            original_templates = self._load_backup_prompt_templates()

            # Reset all agent templates to backup versions
            for agent_type in AgentType:
                if agent_type in original_templates:
                    self.prompt_templates[agent_type] = original_templates[agent_type]

                    # Save cleaned template to main config directory
                    main_file = self.config_dir / f"{agent_type.value}_agent_prompts.yaml"
                    with open(main_file, "w") as f:
                        yaml.dump(original_templates[agent_type], f, default_flow_style=False)

                    logger.info(f"✅ Cleaned guidelines for {agent_type.value}")
                else:
                    logger.warning(f"⚠️ No backup template found for {agent_type.value}")

            # Clear guideline history
            self.guideline_history.clear()

            # Create a fresh version directory
            if self.version_dir.exists():
                import shutil

                shutil.rmtree(self.version_dir)
            self.version_dir.mkdir(parents=True, exist_ok=True)

            # If using point-based system, also clear learned points from default JSON files
            # We clear the default location directly without loading points into memory
            if self.use_point_based:
                default_points_path = Path("meta_agent/point_prompts")
                if default_points_path.exists():
                    logger.info("🧹 Clearing learned points from default JSON files...")
                    # AgentType is already imported at module level
                    for agent_type in AgentType:
                        points_file = default_points_path / f"{agent_type.value}_points.json"
                        if points_file.exists():
                            try:
                                import json
                                with open(points_file, "r") as f:
                                    data = json.load(f)
                                # Remove learned points, keep only original points (if any)
                                filtered_data = [p for p in data if p.get("source") != "learned"]
                                with open(points_file, "w") as f:
                                    json.dump(filtered_data, f, indent=2)
                            except Exception as e:
                                logger.warning(f"Failed to clear learned points from {points_file}: {e}")
                    logger.info("✅ Cleared all learned points from default JSON files")

            logger.info("🎉 Successfully cleaned all learned guidelines!")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to clean guidelines: {e}")
            return False

    def _create_backup_prompts(self) -> None:
        """Create backup copies of original prompts before any updates"""
        backup_dir = self.config_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        agent_files = {
            AgentType.DIAGNOSIS: backup_dir / "diagnosis_agent_prompts.yaml",
            AgentType.LOCALIZATION: backup_dir / "localization_agent_prompts.yaml",
            AgentType.MITIGATION: backup_dir / "mitigation_agent_prompts.yaml",
            AgentType.ROLLBACK: backup_dir / "rollback_agent_prompts.yaml",
        }

        # Create backups from original prompts (if not already backed up)
        for agent_type, backup_file in agent_files.items():
            if not backup_file.exists() and agent_type in self.original_prompts and self.original_prompts[agent_type]:
                try:
                    with open(backup_file, "w") as f:
                        yaml.dump(self.original_prompts[agent_type], f, default_flow_style=False)
                    logger.info(f"✅ Created backup for {agent_type.value} at {backup_file}")
                except Exception as e:
                    logger.error(f"❌ Failed to create backup for {agent_type.value}: {e}")
            elif backup_file.exists():
                logger.debug(f"Backup already exists for {agent_type.value}")

    def _load_backup_prompt_templates(self) -> Dict[AgentType, Dict[str, Any]]:
        """Load original prompt templates from backup files"""
        templates = {}

        # Define backup file paths
        backup_dir = self.config_dir / "backups"

        agent_files = {
            AgentType.DIAGNOSIS: backup_dir / "diagnosis_agent_prompts.yaml",
            AgentType.LOCALIZATION: backup_dir / "localization_agent_prompts.yaml",
            AgentType.MITIGATION: backup_dir / "mitigation_agent_prompts.yaml",
            AgentType.ROLLBACK: backup_dir / "rollback_agent_prompts.yaml",
        }

        # Load each backup file
        for agent_type, backup_file in agent_files.items():
            if backup_file.exists():
                try:
                    with open(backup_file, "r") as f:
                        template = yaml.safe_load(f)
                        templates[agent_type] = template
                        logger.info(f"✅ Loaded backup template for {agent_type.value}")
                except Exception as e:
                    logger.error(f"❌ Failed to load backup for {agent_type.value}: {e}")
            else:
                logger.warning(f"⚠️ Backup file not found: {backup_file}")

        return templates

    def _load_original_prompt_templates(self) -> Dict[AgentType, Dict[str, Any]]:
        """Load original prompt templates without any learned patterns"""
        templates = {}

        # Define original templates for each agent type
        original_templates = {
            AgentType.DIAGNOSIS: {
                "system": """Monitor and diagnose an application consisting of **MANY** microservices. Some or none of the microservices have faults. Get all the pods and deployments to figure out what kind of services are running in the cluster. Carefully identify the whether the faults are present and if they are, and identify what is the root cause of the fault.
Stop diagnosis once you've found the root cause of the faults.
Go as deep as you can into what is causing the issue.
Your instructions to the tools must be clear and concise. Your queries to tools need to be single turn.
Remember to check these, and remember this information: ## Workloads (Applications) - **Pod**: The smallest deployable unit in Kubernetes, representing a single instance of a running application. Can contain one or more tightly coupled containers. - **ReplicaSet**: Ensures that a specified number of pod replicas are running at all times. Often managed indirectly through Deployments. - **Deployment**: Manages the deployment and lifecycle of applications. Provides declarative updates for Pods and ReplicaSets. - **StatefulSet**: Manages stateful applications with unique pod identities and stable storage. Used for workloads like databases. - **DaemonSet**: Ensures that a copy of a specific pod runs on every node in the cluster. Useful for node monitoring agents, log collectors, etc. - **Job**: Manages batch processing tasks that are expected to complete successfully. Ensures pods run to completion. - **CronJob**: Schedules jobs to run at specified times or intervals (similar to cron in Linux).
## Networking - **Service**: Provides a stable network endpoint for accessing a group of pods. Types: ClusterIP, NodePort, LoadBalancer, and ExternalName. - **Ingress**: Manages external HTTP(S) access to services in the cluster. Supports routing and load balancing for HTTP(S) traffic. - **NetworkPolicy**: Defines rules for network communication between pods and other entities. Used for security and traffic control.
## Storage - **PersistentVolume (PV)**: Represents a piece of storage in the cluster, provisioned by an administrator or dynamically. - **PersistentVolumeClaim (PVC)**: Represents a request for storage by a user. Binds to a PersistentVolume. - **StorageClass**: Defines different storage tiers or backends for dynamic provisioning of PersistentVolumes. - **ConfigMap**: Stores configuration data as key-value pairs for applications. - **Secret**: Stores sensitive data like passwords, tokens, or keys in an encrypted format.
## Configuration and Metadata - **Namespace**: Logical partitioning of resources within the cluster for isolation and organization. - **ConfigMap**: Provides non-sensitive configuration data in key-value format. - **Secret**: Stores sensitive configuration data securely. - **ResourceQuota**: Restricts resource usage (e.g., CPU, memory) within a namespace. - **LimitRange**: Enforces minimum and maximum resource limits for containers in a namespace.
## Cluster Management - **Node**: Represents a worker machine in the cluster (virtual or physical). Runs pods and is managed by the control plane. - **ClusterRole and Role**: Define permissions for resources at the cluster or namespace level. - **ClusterRoleBinding and RoleBinding**: Bind roles to users or groups for authorization. - **ServiceAccount**: Associates processes in pods with permissions for accessing the Kubernetes API.
After you finished, submit "Yes" to denote that there's an incident in the cluster. Submit "No" to denote that there is no incidents identified.""",
                "user": "",
            },
            AgentType.LOCALIZATION: {
                "system": """You are a localization agent responsible for identifying the specific location and scope of faults in a microservices application. Your task is to pinpoint exactly where the problem is occurring.

## Your Responsibilities:
1. **Identify Affected Services**: Determine which specific microservices are experiencing issues
2. **Locate Fault Boundaries**: Understand the scope and impact of the fault
3. **Analyze Dependencies**: Map out how services interact and where failures propagate
4. **Provide Precise Location**: Give specific details about where the fault is occurring

## Key Tools for Localization:
- **kubectl get pods**: Check pod status and health
- **kubectl logs**: Examine service logs for error patterns
- **kubectl describe**: Get detailed information about resources
- **kubectl exec**: Execute commands inside pods for deeper investigation
- **prometheus_query**: Query metrics to understand service behavior
- **jaeger_query**: Trace requests to see where failures occur

## Localization Strategy:
1. Start with broad service discovery
2. Narrow down to specific failing components
3. Analyze logs and metrics for error patterns
4. Trace request flows to identify failure points
5. Document the exact location and scope of the fault

After you have identified the specific location and scope of the fault, submit "Yes" to indicate successful localization, or "No" if you cannot determine the fault location.""",
                "user": "",
            },
            AgentType.MITIGATION: {
                "system": """You are a mitigation agent responsible for implementing fixes for identified faults in a microservices application. Your task is to resolve the issues that have been diagnosed and localized.

## Your Responsibilities:
1. **Implement Fixes**: Apply appropriate solutions to resolve the identified faults
2. **Validate Changes**: Ensure that implemented fixes actually resolve the issues
3. **Monitor Impact**: Verify that fixes don't introduce new problems
4. **Document Actions**: Keep track of what changes were made

## Key Tools for Mitigation:
- **kubectl apply**: Apply configuration changes
- **kubectl patch**: Make targeted updates to resources
- **kubectl exec**: Execute commands to fix issues inside pods
- **kubectl rollout**: Manage deployments and rollouts
- **kubectl delete**: Remove problematic resources
- **kubectl create**: Create new resources as needed

## Mitigation Strategy:
1. Understand the root cause from diagnosis and localization
2. Plan the appropriate fix based on the fault type
3. Implement the fix using appropriate tools
4. Verify that the fix resolves the issue
5. Monitor for any side effects or new issues

After you have successfully implemented fixes and verified they resolve the issues, submit "Yes" to indicate successful mitigation, or "No" if the mitigation was unsuccessful.""",
                "user": "",
            },
            AgentType.ROLLBACK: {
                "system": """You are a rollback agent responsible for reverting changes when mitigation attempts fail or cause additional problems. Your task is to restore the system to a previous working state.

## Your Responsibilities:
1. **Identify Rollback Needs**: Determine when rollback is necessary
2. **Execute Rollbacks**: Revert changes to restore system stability
3. **Validate Restoration**: Ensure the system returns to a working state
4. **Document Rollback Actions**: Keep track of what was reverted

## Key Tools for Rollback:
- **kubectl rollout undo**: Undo recent deployments
- **kubectl patch**: Revert configuration changes
- **kubectl apply**: Apply previous working configurations
- **kubectl delete**: Remove problematic resources
- **kubectl create**: Restore deleted resources

## Rollback Strategy:
1. Assess the current system state and issues
2. Identify what changes need to be reverted
3. Execute appropriate rollback commands
4. Verify that the system returns to a stable state
5. Monitor for continued stability

After you have successfully rolled back changes and restored system stability, submit "Yes" to indicate successful rollback, or "No" if the rollback was unsuccessful.""",
                "user": "",
            },
        }

        return original_templates

    def save_guideline_history(self, filepath: str) -> None:
        """Save guideline history to file"""
        history_data = [asdict(update) for update in self.guideline_history]

        with open(filepath, "w") as f:
            json.dump(history_data, f, indent=2, default=str)

        logger.info(f"Saved guideline history to {filepath}")
