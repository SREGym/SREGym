"""
Point-Based Prompt System

Converts prompts and insights into discrete, validated points that can be
individually tracked, validated, and managed without conflicts.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from .trace_collector import AgentType

logger = logging.getLogger(__name__)


@dataclass
class PromptPoint:
    """A discrete instruction or guidance point"""

    id: str
    content: str
    source: str  # "original" | "learned" | "merged"
    category: str  # "tool_usage" | "workflow" | "warning" | "example" | "reference"
    priority: int = 5  # 1-10, higher = more important
    verified: bool = False
    verification_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    conflicts_with: List[str] = field(default_factory=list)
    replaces: Optional[str] = None  # ID of point this replaces
    replaced_by: Optional[str] = None  # ID of point that replaces this
    active: bool = True
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PromptPoint":
        """Create from dictionary"""
        return cls(**data)

    def mark_verified(self):
        """Mark point as verified after sufficient successful uses"""
        if self.verification_count >= 3 and self.success_count >= 2:
            self.verified = True
            self.last_updated = datetime.now().isoformat()
            logger.info(f"✅ Point {self.id} marked as VERIFIED")

    def should_remove(self) -> bool:
        """Check if point should be removed due to poor performance"""
        # Remove if consistently failing
        if self.failure_count >= 2 and self.success_count == 0:
            return True
        # Remove if replaced
        if self.replaced_by:
            return True
        return False


class PointBasedPromptManager:
    """Manages prompts as discrete, validated points"""

    def __init__(self, storage_path: str = "meta_agent/point_prompts", use_llm_detection: bool = True, use_llm_usage_detection: bool = True, skip_load: bool = False, use_llm_primary: bool = True):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.points: Dict[AgentType, List[PromptPoint]] = {
            agent_type: [] for agent_type in AgentType
        }
        self.use_llm_detection = use_llm_detection
        self.use_llm_usage_detection = use_llm_usage_detection
        self.use_llm_primary = use_llm_primary  # If True, use LLM as primary method instead of heuristics
        self._llm_backend = None
        # Rate limiting for LLM calls
        self._last_llm_call_time = 0.0
        self._llm_call_delay = 2.0  # Minimum seconds between LLM calls
        # Conflict detection cache and rate limiting
        self._conflict_cache: Dict[Tuple[str, str], bool] = {}
        self._last_llm_conflict_call_time = 0.0
        self._llm_conflict_call_delay = 0.5  # Minimum delay between LLM conflict checks (seconds)
        if not skip_load:
            self._load_points()

    def _load_points(self):
        """Load points from storage (only learned points, no original points)"""
        for agent_type in AgentType:
            points_file = self.storage_path / f"{agent_type.value}_points.json"
            if points_file.exists():
                try:
                    with open(points_file, "r") as f:
                        data = json.load(f)
                        # Only load learned points (source='learned')
                        # Original prompts are NOT stored as points
                        learned_points = [
                            PromptPoint.from_dict(p) for p in data 
                            if p.get("source") == "learned"
                        ]
                        self.points[agent_type] = learned_points
                    if learned_points:
                        logger.info(f"Loaded {len(learned_points)} learned points for {agent_type.value} from {points_file}")
                except Exception as e:
                    logger.error(f"Failed to load points for {agent_type.value}: {e}")
            else:
                # No points file exists - this is expected for a fresh run (Round 1)
                # Don't log this as it's expected behavior
                pass

    def _save_points(self, agent_type: AgentType):
        """Save points to storage"""
        points_file = self.storage_path / f"{agent_type.value}_points.json"
        data = [p.to_dict() for p in self.points[agent_type]]
        with open(points_file, "w") as f:
            json.dump(data, f, indent=2)

    def clear_learned_points(self, agent_type: Optional[AgentType] = None):
        """Clear all learned points (points with source='learned')
        
        Args:
            agent_type: If provided, only clear learned points for this agent type.
                       If None, clear learned points for all agent types.
        """
        if agent_type is None:
            # Clear for all agent types
            for at in AgentType:
                self.points[at] = [p for p in self.points[at] if p.source != "learned"]
                self._save_points(at)
            logger.info("Cleared all learned points for all agent types")
        else:
            # Clear for specific agent type
            self.points[agent_type] = [p for p in self.points[agent_type] if p.source != "learned"]
            self._save_points(agent_type)
            logger.info(f"Cleared learned points for {agent_type.value}")

    def parse_original_prompt(self, agent_type: AgentType, prompt: Dict[str, Any]) -> List[PromptPoint]:
        """Parse original prompt into discrete points
        
        Only parses if no original points exist yet to avoid duplicates.
        """
        # Check if we already have original points for this agent type
        existing_original_points = [
            p for p in self.points[agent_type] 
            if p.source == "original"
        ]
        
        if existing_original_points:
            logger.info(
                f"Skipping parsing original prompt for {agent_type.value}: "
                f"{len(existing_original_points)} original points already exist"
            )
            return existing_original_points
        
        points = []
        system_prompt = prompt.get("system", "")

        # Extract sections
        sections = self._extract_sections(system_prompt)

        for section_title, section_content in sections.items():
            # Parse section into points
            section_points = self._parse_section(section_title, section_content, "original")
            points.extend(section_points)

        # Deduplicate points by content before adding
        existing_contents = {p.content for p in self.points[agent_type]}
        new_points = [p for p in points if p.content not in existing_contents]
        
        if len(new_points) < len(points):
            logger.info(
                f"Deduplicated points: {len(points)} parsed, {len(new_points)} new "
                f"({len(points) - len(new_points)} duplicates skipped)"
            )
        
        # Store only new points
        self.points[agent_type].extend(new_points)
        self._save_points(agent_type)
        logger.info(f"Parsed {len(new_points)} points from original prompt for {agent_type.value}")

        return new_points

    def _extract_sections(self, text: str) -> Dict[str, str]:
        """Extract sections from prompt text"""
        sections = {}
        current_section = "main"
        current_content = []

        lines = text.split("\n")
        for line in lines:
            # Check for section header (## Title)
            if line.strip().startswith("##"):
                # Save previous section
                if current_content:
                    sections[current_section] = "\n".join(current_content)
                # Start new section
                current_section = line.strip().replace("##", "").strip().lower()
                current_content = []
            else:
                current_content.append(line)

        # Save last section
        if current_content:
            sections[current_section] = "\n".join(current_content)

        return sections

    def _parse_section(self, section_title: str, content: str, source: str) -> List[PromptPoint]:
        """Parse a section into individual points"""
        points = []

        # Split by bullet points, numbered lists, or paragraphs
        # Look for patterns like:
        # - Point 1
        # - Point 2
        # or
        # 1. Point 1
        # 2. Point 2
        # or paragraphs separated by double newlines

        # Try bullet points first
        bullet_pattern = r"^[-*•]\s+(.+)$"
        bullets = re.findall(bullet_pattern, content, re.MULTILINE)
        if bullets:
            for bullet in bullets:
                point = PromptPoint(
                    id=str(uuid.uuid4()),
                    content=bullet.strip(),
                    source=source,
                    category=self._infer_category(section_title, bullet),
                    priority=self._infer_priority(section_title, bullet),
                )
                points.append(point)
            return points

        # Try numbered list
        numbered_pattern = r"^\d+\.\s+(.+)$"
        numbered = re.findall(numbered_pattern, content, re.MULTILINE)
        if numbered:
            for item in numbered:
                point = PromptPoint(
                    id=str(uuid.uuid4()),
                    content=item.strip(),
                    source=source,
                    category=self._infer_category(section_title, item),
                    priority=self._infer_priority(section_title, item),
                )
                points.append(point)
            return points

        # Fall back to paragraphs
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        for para in paragraphs:
            # Skip if too short or just whitespace
            if len(para) < 20:
                continue
            point = PromptPoint(
                id=str(uuid.uuid4()),
                content=para.strip(),
                source=source,
                category=self._infer_category(section_title, para),
                priority=self._infer_priority(section_title, para),
            )
            points.append(point)

        return points

    def _infer_category(self, section_title: str, content: str) -> str:
        """Infer category from section title and content"""
        section_lower = section_title.lower()
        content_lower = content.lower()

        if "tool" in section_lower or "tool" in content_lower:
            return "tool_usage"
        elif "workflow" in section_lower or "step" in section_lower:
            return "workflow"
        elif "warning" in section_lower or "avoid" in content_lower or "don't" in content_lower:
            return "warning"
        elif "example" in section_lower:
            return "example"
        elif "reference" in section_lower or "kubernetes" in content_lower:
            return "reference"
        else:
            return "general"

    def _infer_priority(self, section_title: str, content: str) -> int:
        """Infer priority from section title and content"""
        section_lower = section_title.lower()
        content_lower = content.lower()

        # High priority indicators
        if "critical" in content_lower or "must" in content_lower or "required" in content_lower:
            return 9
        elif "important" in content_lower or "should" in content_lower:
            return 7
        elif "warning" in section_lower or "avoid" in content_lower:
            return 8
        elif "example" in section_lower:
            return 3
        elif "reference" in section_lower:
            return 2
        else:
            return 5

    def add_learned_insight(self, agent_type: AgentType, insight: Dict[str, Any]) -> PromptPoint:
        """Add a learned insight as a new point
        
        Checks for duplicates before adding to avoid accumulating duplicate points.
        """
        content = insight.get("content", "")
        
        # Check for duplicate content (exact match)
        existing_point = next(
            (p for p in self.points[agent_type] if p.content == content),
            None
        )
        
        if existing_point:
            logger.debug(
                f"Skipping duplicate learned insight for {agent_type.value}: "
                f"point {existing_point.id[:8]}... already exists with same content"
            )
            return existing_point
        
        # Map insight type to point category
        insight_type = insight.get("type", "add_guidance")
        category_map = {
            "add_guidance": "general",
            "add_warning": "warning",
            "add_recommendation": "tool_usage",
            "add_caution": "warning",
            "add_thinking_guidance": "workflow",
            "tool_usage": "tool_usage",
            "workflow": "workflow",
            "warning": "warning",
            "general": "general",
        }
        category = category_map.get(insight_type, "general")
        
        point = PromptPoint(
            id=str(uuid.uuid4()),
            content=content,
            source="learned",
            category=category,
            priority=insight.get("priority", 6),  # Learned insights default to higher priority
            metadata=insight.get("metadata", {}),
        )

        self.points[agent_type].append(point)
        self._save_points(agent_type)
        logger.info(f"Added learned insight point {point.id} for {agent_type.value}")

        return point

    def detect_conflicts(
        self, agent_type: AgentType, new_point_ids: Optional[List[str]] = None
    ) -> Dict[str, List[str]]:
        """
        Detect conflicts between points.
        
        If new_point_ids is provided, only checks conflicts involving new points.
        Otherwise, checks all points (for full scan, but should be avoided).
        
        Args:
            agent_type: The agent type to check
            new_point_ids: Optional list of new point IDs. If provided, only checks
                         conflicts between new points and all existing points.
        
        Returns:
            Dictionary mapping point IDs to lists of conflicting point IDs
        """
        points = [p for p in self.points[agent_type] if p.active]
        conflicts = {}

        # Always prefer incremental detection if we can identify new points
        # If new_point_ids not provided but we can infer them, do so
        if not new_point_ids and len(points) > 10:
            # For large point sets, try to identify recently added points
            # Points added in the last hour are likely "new"
            from datetime import datetime, timedelta
            recent_threshold = datetime.now() - timedelta(hours=1)
            new_point_ids = [
                p.id for p in points 
                if p.created_at and datetime.fromisoformat(p.created_at) > recent_threshold
            ]
            if new_point_ids:
                logger.info(f"Auto-detected {len(new_point_ids)} recent points for incremental conflict detection")

        if new_point_ids:
            # Incremental detection: Only check conflicts involving new points
            new_points = {p.id: p for p in points if p.id in new_point_ids}
            existing_points = [p for p in points if p.id not in new_point_ids]
            
            logger.debug(
                f"Incremental conflict detection: {len(new_points)} new points vs {len(existing_points)} existing points"
            )
            
            # Check: new vs existing (with rate limiting for LLM calls)
            for new_id, new_point in new_points.items():
                if new_id not in conflicts:
                    conflicts[new_id] = []
                
                for existing_point in existing_points:
                    # Check cache first
                    cache_key = tuple(sorted([new_id, existing_point.id]))
                    if cache_key in self._conflict_cache:
                        if self._conflict_cache[cache_key]:
                            conflicts[new_id].append(existing_point.id)
                            if existing_point.id not in conflicts:
                                conflicts[existing_point.id] = []
                            conflicts[existing_point.id].append(new_id)
                        continue
                    
                    # Rate limit LLM calls
                    self._rate_limit_llm_conflict_call()
                    
                    if self._is_conflict(new_point, existing_point):
                        conflicts[new_id].append(existing_point.id)
                        if existing_point.id not in conflicts:
                            conflicts[existing_point.id] = []
                        conflicts[existing_point.id].append(new_id)
                        # Cache the result
                        self._conflict_cache[cache_key] = True
                    else:
                        # Cache negative result too
                        self._conflict_cache[cache_key] = False
            
            # Check: new vs new (in case multiple new insights conflict)
            new_point_list = list(new_points.values())
            for i, point1 in enumerate(new_point_list):
                for point2 in new_point_list[i + 1 :]:
                    # Check cache first
                    cache_key = tuple(sorted([point1.id, point2.id]))
                    if cache_key in self._conflict_cache:
                        if self._conflict_cache[cache_key]:
                            if point1.id not in conflicts:
                                conflicts[point1.id] = []
                            if point2.id not in conflicts:
                                conflicts[point2.id] = []
                            conflicts[point1.id].append(point2.id)
                            conflicts[point2.id].append(point1.id)
                        continue
                    
                    # Rate limit LLM calls
                    self._rate_limit_llm_conflict_call()
                    
                    if self._is_conflict(point1, point2):
                        if point1.id not in conflicts:
                            conflicts[point1.id] = []
                        if point2.id not in conflicts:
                            conflicts[point2.id] = []
                        conflicts[point1.id].append(point2.id)
                        conflicts[point2.id].append(point1.id)
                        # Cache the result
                        self._conflict_cache[cache_key] = True
                    else:
                        # Cache negative result too
                        self._conflict_cache[cache_key] = False
        else:
            # Full scan (inefficient, but kept for backward compatibility)
            logger.warning(
                f"Full conflict detection for {agent_type.value}: {len(points)} points = {len(points) * (len(points) - 1) // 2} pairs. "
                "Consider using incremental detection with new_point_ids."
            )
            
            for i, point1 in enumerate(points):
                if point1.id not in conflicts:
                    conflicts[point1.id] = []

                for point2 in points[i + 1 :]:
                    # Check cache first
                    cache_key = tuple(sorted([point1.id, point2.id]))
                    if cache_key in self._conflict_cache:
                        if self._conflict_cache[cache_key]:
                            conflicts[point1.id].append(point2.id)
                            if point2.id not in conflicts:
                                conflicts[point2.id] = []
                            conflicts[point2.id].append(point1.id)
                        continue
                    
                    # Rate limit LLM calls
                    self._rate_limit_llm_conflict_call()
                    
                    if self._is_conflict(point1, point2):
                        conflicts[point1.id].append(point2.id)
                        if point2.id not in conflicts:
                            conflicts[point2.id] = []
                        conflicts[point2.id].append(point1.id)
                        # Cache the result
                        self._conflict_cache[cache_key] = True
                    else:
                        # Cache negative result too
                        self._conflict_cache[cache_key] = False

        return conflicts

    def _is_conflict(self, point1: PromptPoint, point2: PromptPoint) -> bool:
        """Check if two points conflict - uses fast checks first, LLM only if needed"""
        # Fast check 1: Direct tool usage conflicts (exact matching, no LLM needed)
        if point1.category == "tool_usage" and point2.category == "tool_usage":
            tool1 = self._extract_tool_name(point1.content)
            tool2 = self._extract_tool_name(point2.content)
            if tool1 and tool2 and tool1 == tool2:
                # Check if one says use and other says avoid
                if ("avoid" in point1.content.lower() or "don't" in point1.content.lower()) and (
                    "use" in point2.content.lower() or "should" in point2.content.lower()
                ):
                    return True
                if ("avoid" in point2.content.lower() or "don't" in point2.content.lower()) and (
                    "use" in point1.content.lower() or "should" in point1.content.lower()
                ):
                    return True

        # Fast check 2: Semantic conflict detection (pattern matching, no LLM needed)
        if self._is_semantic_conflict(point1.content, point2.content):
            return True

        # Fast check 3: Different categories are unlikely to conflict (skip LLM)
        if point1.category != point2.category and point1.category not in ["tool_usage", "workflow"]:
            return False

        # Only use LLM for ambiguous cases that passed fast checks
        # This significantly reduces API calls
        if self.use_llm_detection:
            llm_conflict = self._is_llm_conflict(point1, point2)
            if llm_conflict is not None:  # LLM gave a definitive answer
                return llm_conflict

        return False

    def _extract_tool_name(self, content: str) -> Optional[str]:
        """Extract tool name from content"""
        # Known tool names (in order of specificity - longer names first)
        known_tools = [
            "exec_kubectl_cmd_safely",
            "exec_read_only_kubectl_cmd",
            "get_previous_rollbackable_cmd",
            "get_dependency_graph",
            "get_metrics",
            "get_traces",
            "get_services",
            "get_operations",
            "get_resource_uid",
            "rollback_command",
            "submit_tool",
            "f_submit_tool",
            "r_submit_tool",
            "wait_tool",
        ]
        
        content_lower = content.lower()
        
        # First, try to match known tools (most specific first)
        for tool in known_tools:
            # Match tool name with optional backticks, quotes, or word boundaries
            pattern = rf"`?{re.escape(tool)}`?|{re.escape(tool)}"
            if re.search(pattern, content_lower):
                return tool
        
        # Fallback: Look for tool names with underscores (pattern: word_word or word_word_word)
        tool_pattern = r"`?(\w+_\w+(?:_\w+)*)`?"
        match = re.search(tool_pattern, content)
        if match:
            return match.group(1)
        
        # NEW: Map kubectl commands to kubectl tools
        # If point mentions "kubectl" commands, map to exec_kubectl tools
        if "kubectl" in content_lower:
            # Check if it's a read-only command (get, describe, logs, etc.)
            read_only_patterns = [
                r"kubectl\s+(get|describe|logs|top|api-resources|explain|version|config)",
                r"kubectl\s+get\s+",
                r"kubectl\s+describe\s+",
            ]
            for pattern in read_only_patterns:
                if re.search(pattern, content_lower):
                    return "exec_read_only_kubectl_cmd"
            
            # Otherwise, assume it might need write access
            return "exec_kubectl_cmd_safely"
        
        return None

    def _is_semantic_conflict(self, content1: str, content2: str) -> bool:
        """Check for semantic conflict (simplified version)"""
        # This is a simplified check - could use LLM for better detection
        content1_lower = content1.lower()
        content2_lower = content2.lower()

        # Check for direct contradictions
        contradictions = [
            ("use", "avoid"),
            ("should", "should not"),
            ("must", "must not"),
            ("do", "don't"),
            ("always", "never"),
        ]

        for pos, neg in contradictions:
            if pos in content1_lower and neg in content2_lower:
                # Check if talking about similar things
                if self._similar_topic(content1, content2):
                    return True
            if pos in content2_lower and neg in content1_lower:
                if self._similar_topic(content1, content2):
                    return True

        return False

    def _similar_topic(self, text1: str, text2: str) -> bool:
        """Check if two texts are about similar topics (simplified)"""
        # Extract key words (tools, actions, etc.)
        words1 = set(re.findall(r"\b\w+\b", text1.lower()))
        words2 = set(re.findall(r"\b\w+\b", text2.lower()))

        # Check for significant overlap
        overlap = len(words1 & words2)
        total_unique = len(words1 | words2)

        if total_unique == 0:
            return False

        similarity = overlap / total_unique
        return similarity > 0.3  # 30% word overlap suggests similar topic

    def _get_llm_backend(self):
        """Get or initialize LLM backend for conflict detection"""
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
                        temperature=0.0,  # Low temperature for deterministic conflict detection
                        reasoning_effort="",
                        thinking_tools="",
                        thinking_budget_tools=0,
                        max_tokens=2000,  # Increased for usage detection (batched points)
                    )
                    logger.debug("Initialized LLM backend for conflict detection and usage detection")
                else:
                    logger.warning("No API key found for LLM conflict detection, using fallback methods")
                    self._llm_backend = None
            except Exception as e:
                logger.warning(f"Failed to initialize LLM backend for conflict detection: {e}")
                self._llm_backend = None

        return self._llm_backend

    def _extract_json_with_bracket_matching(self, json_str: str) -> Optional[Dict[str, Any]]:
        """
        Extract complete JSON object using bracket matching.
        Handles nested objects and strings properly.
        """
        if not json_str or not json_str.strip().startswith('{'):
            return None
        
        bracket_count = 0
        in_string = False
        escape_next = False
        start_idx = 0
        end_idx = len(json_str)
        
        for i, char in enumerate(json_str):
            if escape_next:
                escape_next = False
                continue
            
            if char == '\\':
                escape_next = True
                continue
            
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            
            if not in_string:
                if char == '{':
                    if bracket_count == 0:
                        start_idx = i
                    bracket_count += 1
                elif char == '}':
                    bracket_count -= 1
                    if bracket_count == 0:
                        end_idx = i + 1
                        break
        
        if bracket_count == 0 and end_idx > start_idx:
            try:
                return json.loads(json_str[start_idx:end_idx])
            except json.JSONDecodeError:
                pass
        
        return None

    def _extract_json_from_response(self, response_text: str) -> Optional[Dict[str, Any]]:
        """
        Robustly extract JSON from LLM response text.
        Handles code blocks, nested JSON, and various formatting issues.
        """
        if not response_text:
            return None
        
        # Strategy 1: Look for JSON in code blocks (```json ... ```)
        # Find code block markers first, then extract JSON with bracket matching
        code_block_match = re.search(r'```json\s*(\{.*)', response_text, re.DOTALL)
        if code_block_match:
            json_start = code_block_match.start(1)
            # Find the matching closing ```
            code_block_end = response_text.find('```', json_start)
            if code_block_end != -1:
                json_str = response_text[json_start:code_block_end].strip()
                # Use bracket matching to extract complete JSON
                result = self._extract_json_with_bracket_matching(json_str)
                if result:
                    return result
        
        # Strategy 2: Look for JSON object with proper bracket matching
        start_idx = response_text.find('{')
        if start_idx != -1:
            result = self._extract_json_with_bracket_matching(response_text[start_idx:])
            if result:
                return result
        
        # Strategy 3: Try to parse the entire response (in case it's pure JSON)
        try:
            return json.loads(response_text.strip())
        except json.JSONDecodeError:
            pass
        
        # Strategy 4: Try to find and extract JSON with a more lenient approach
        # Look for patterns like {"conflicts": ...}
        json_pattern = re.search(r'\{\s*"conflicts"\s*:\s*(?:true|false)\s*,\s*"reason"\s*:\s*"[^"]*"\s*\}', response_text, re.DOTALL)
        if json_pattern:
            try:
                # Try to fix common issues: unescaped newlines in strings
                json_str = json_pattern.group(0)
                # Replace unescaped newlines in string values (between quotes)
                json_str = re.sub(r'(?<!\\)"(?P<content>[^"]*)\n(?P<rest>[^"]*)"', r'"\g<content>\\n\g<rest>"', json_str)
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
        
        return None

    def _rate_limit_llm_conflict_call(self):
        """Ensure minimum delay between LLM conflict detection calls to avoid rate limiting"""
        current_time = time.time()
        time_since_last_call = current_time - self._last_llm_conflict_call_time
        if time_since_last_call < self._llm_conflict_call_delay:
            sleep_time = self._llm_conflict_call_delay - time_since_last_call
            time.sleep(sleep_time)
        self._last_llm_conflict_call_time = time.time()

    def _is_llm_conflict(self, point1: PromptPoint, point2: PromptPoint) -> Optional[bool]:
        """
        Use LLM to detect semantic conflicts between two points.
        
        Returns:
            True if conflict detected, False if no conflict, None if LLM unavailable
        """
        llm_backend = self._get_llm_backend()
        if not llm_backend:
            return None  # LLM not available, fallback to other methods

        # Add retry logic for rate limiting
        max_retries = 3
        retry_delay = 2.0  # Start with 2 seconds
        
        for attempt in range(max_retries):
            try:
                prompt = f"""You are an expert at analyzing AI agent prompt guidelines and instructions.

Compare these two instructions and determine if they CONFLICT with each other.

**Instruction 1:**
{point1.content}

**Instruction 2:**
{point2.content}

Do these instructions CONFLICT? Consider:
- Do they give opposite or contradictory advice?
- Are they mutually exclusive (following one prevents following the other)?
- Would following one instruction make it impossible or counterproductive to follow the other?
- Do they recommend different approaches for the same task that cannot both be followed?

IMPORTANT: 
- Complementary instructions (that can both be followed) are NOT conflicts
- Instructions about different topics are NOT conflicts
- Only mark as conflict if they are truly contradictory or mutually exclusive

Respond with ONLY a JSON object in this exact format:
{{
    "conflicts": true or false,
    "reason": "brief explanation of why they conflict or don't conflict"
}}"""

                response = llm_backend.inference(messages=[prompt], system_prompt=None)
                break  # Success, exit retry loop
            except Exception as e:
                error_str = str(e).lower()
                # Check if it's a rate limit error
                if "rate" in error_str or "limit" in error_str or "429" in error_str:
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)  # Exponential backoff
                        logger.warning(f"Rate limited during conflict detection. Retrying in {wait_time:.1f}s... (Attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Max retries exceeded for conflict detection. Unable to complete the request.")
                        return None  # Fallback to other methods
                else:
                    # Not a rate limit error, re-raise
                    raise
        
        # Extract content from response (may be AIMessage object or string)
        if 'response' not in locals():
            return None  # All retries failed
        
        try:
            if hasattr(response, 'content'):
                response_text = response.content
            elif isinstance(response, str):
                response_text = response
            else:
                # Try to convert to string
                response_text = str(response)
            
            # Parse JSON response with robust extraction
            result = self._extract_json_from_response(response_text)
            
            if result is None:
                logger.warning(f"Could not extract JSON from LLM conflict detection response")
                logger.debug(f"LLM response was: {response_text[:500]}")
                return None  # Fallback to other methods
            
            conflicts = result.get("conflicts", False)
            reason = result.get("reason", "")
            
            if conflicts:
                logger.debug(f"LLM detected conflict between points: {reason}")
            else:
                logger.debug(f"LLM found no conflict: {reason}")
            
            return conflicts

        except Exception as e:
            logger.warning(f"Error during LLM conflict detection: {e}")
            if 'response_text' in locals():
                logger.debug(f"LLM response was: {response_text[:500]}")
            return None  # Fallback to other methods

    def resolve_conflicts(self, agent_type: AgentType) -> List[PromptPoint]:
        """Resolve conflicts and return active points"""
        conflicts = self.detect_conflicts(agent_type)
        points = [p for p in self.points[agent_type] if p.active]

        # Resolve each conflict
        for point_id, conflict_ids in conflicts.items():
            if not conflict_ids:
                continue

            point = next((p for p in points if p.id == point_id), None)
            if not point:
                continue

            conflicting_points = [p for p in points if p.id in conflict_ids]

            # Select winner
            winner = self._select_winner([point] + conflicting_points)

            # Mark losers as replaced
            for loser in [point] + conflicting_points:
                if loser.id != winner.id:
                    loser.replaced_by = winner.id
                    loser.active = False
                    winner.replaces = loser.id
                    logger.info(f"Point {loser.id} replaced by {winner.id} due to conflict")

        # Remove inactive points
        active_points = [p for p in points if p.active and not p.replaced_by]

        # Remove bad points
        active_points = [p for p in active_points if not p.should_remove()]

        self.points[agent_type] = active_points
        self._save_points(agent_type)

        return active_points

    def _select_winner(self, points: List[PromptPoint]) -> PromptPoint:
        """Select the best point among conflicting ones"""
        # Priority order:
        # 1. Verified > Unverified
        # 2. Higher success rate
        # 3. Learned (if verified) > Original
        # 4. Higher priority
        # 5. More recent

        def score(point: PromptPoint) -> Tuple[int, float, int, int, int]:
            verified_score = 1 if point.verified else 0
            success_rate = (
                point.success_count / point.verification_count if point.verification_count > 0 else 0
            )
            source_score = 2 if (point.source == "learned" and point.verified) else (1 if point.source == "learned" else 0)
            priority_score = point.priority
            # More recent = higher score (convert timestamp to comparable int)
            recency_score = int(datetime.fromisoformat(point.created_at).timestamp())

            return (verified_score, success_rate, source_score, priority_score, recency_score)

        return max(points, key=score)

    def rebuild_prompt(self, agent_type: AgentType) -> Dict[str, Any]:
        """Rebuild prompt YAML from validated points"""
        active_points = self.resolve_conflicts(agent_type)

        # Group by category
        by_category: Dict[str, List[PromptPoint]] = {}
        for point in active_points:
            if point.category not in by_category:
                by_category[point.category] = []
            by_category[point.category].append(point)

        # Sort points: verified first, then by priority
        for category in by_category:
            by_category[category].sort(
                key=lambda p: (not p.verified, -p.priority, -p.success_count)
            )

        # Build system prompt
        system_parts = []

        # Core instructions (high priority, verified)
        core_points = [p for p in active_points if p.priority >= 8 and p.verified]
        if core_points:
            system_parts.append("## Core Instructions")
            for point in core_points:
                system_parts.append(f"- {point.content}")

        # Tool usage
        if "tool_usage" in by_category:
            system_parts.append("\n## Tool Usage Guidelines")
            for point in by_category["tool_usage"]:
                marker = "✅" if point.verified else "⚠️"
                system_parts.append(f"{marker} {point.content}")

        # Workflow
        if "workflow" in by_category:
            system_parts.append("\n## Workflow Guidelines")
            for point in by_category["workflow"]:
                marker = "✅" if point.verified else "⚠️"
                system_parts.append(f"{marker} {point.content}")

        # Warnings
        if "warning" in by_category:
            system_parts.append("\n## Important Warnings")
            for point in by_category["warning"]:
                marker = "✅" if point.verified else "⚠️"
                system_parts.append(f"{marker} {point.content}")

        # Examples
        if "example" in by_category:
            system_parts.append("\n## Examples")
            for point in by_category["example"]:
                system_parts.append(f"- {point.content}")

        # Reference (keep at end)
        if "reference" in by_category:
            system_parts.append("\n## Reference Information")
            for point in by_category["reference"]:
                system_parts.append(f"- {point.content}")

        return {"system": "\n\n".join(system_parts)}

    def identify_used_points(self, agent_type: AgentType, trace: Any) -> List[str]:
        """
        Analyze a trace and identify which points were actually used.
        
        Args:
            agent_type: The agent type for this trace
            trace: AgentTrace object from trace_collector
            
        Returns:
            List of point IDs that were likely used in this trace
        """
        if agent_type not in self.points:
            logger.debug(f"No points registered for {agent_type.value}, skipping identification")
            return []
        
        used_point_ids = []
        active_points = [p for p in self.points[agent_type] if p.active]
        
        if not active_points:
            logger.info(
                f"Identified 0 used points out of 0 active points for {agent_type.value} "
                f"(no active points available)"
            )
            return []
        
        # Extract trace information
        tool_calls = getattr(trace, 'tool_calls', []) or []
        thinking_steps = getattr(trace, 'thinking_steps', []) or []
        
        # Safely extract tool names
        tool_names = []
        for tc in tool_calls:
            if hasattr(tc, 'tool_name'):
                tool_names.append(tc.tool_name)
            elif isinstance(tc, dict):
                tool_names.append(tc.get('tool_name', ''))
        
        # Safely extract reasoning texts
        reasoning_texts = []
        for ts in thinking_steps:
            if hasattr(ts, 'reasoning') and hasattr(ts, 'justification'):
                reasoning_texts.append(f"{ts.reasoning} {ts.justification}")
            elif isinstance(ts, dict):
                reasoning_texts.append(f"{ts.get('reasoning', '')} {ts.get('justification', '')}")
        
        all_trace_text = " ".join(tool_names + reasoning_texts).lower()
        
        # Prepare trace summary for LLM (if enabled)
        trace_summary = self._prepare_trace_summary(trace, tool_names, reasoning_texts) if self.use_llm_usage_detection else None
        
        # Match points to trace
        if self.use_llm_primary and self.use_llm_usage_detection and trace_summary:
            # PRIMARY MODE: Use LLM as primary method, with heuristics only for obvious matches
            heuristically_matched = set()
            points_for_llm = []
            
            for point in active_points:
                point_content_lower = point.content.lower()
                
                # Only use heuristics for very obvious matches (exact tool name match)
                # This reduces false positives while still catching clear cases
                if point.category == "tool_usage":
                    tool_name = self._extract_tool_name(point.content)
                    if tool_name and tool_name in tool_names:
                        # Exact tool match - very confident, use heuristic
                        used_point_ids.append(point.id)
                        heuristically_matched.add(point.id)
                        logger.debug(f"Point {point.id} matched via exact tool usage: {tool_name}")
                        continue
                
                # For all other points, use LLM
                points_for_llm.append(point)
            
            # Use LLM for all non-obvious points
            if points_for_llm:
                llm_matched = self._llm_identify_used_points(points_for_llm, trace_summary, agent_type)
                for point_id in llm_matched:
                    if point_id not in heuristically_matched:
                        used_point_ids.append(point_id)
                        logger.debug(f"Point {point_id} matched via LLM usage detection (primary mode)")
        else:
            # FALLBACK MODE: Use heuristics first, then LLM for ambiguous points
            heuristically_matched = set()
            ambiguous_points = []
            
            for point in active_points:
                point_content_lower = point.content.lower()
                
                # Method 1: Tool-based matching (for tool_usage points) - fast, exact
                if point.category == "tool_usage":
                    tool_name = self._extract_tool_name(point.content)
                    if tool_name and tool_name in tool_names:
                        used_point_ids.append(point.id)
                        heuristically_matched.add(point.id)
                        logger.debug(f"Point {point.id} matched via tool usage: {tool_name}")
                        continue
                
                # Method 2: Keyword matching (check if point mentions tools/actions used in trace)
                point_keywords = set(re.findall(r'\b\w+\b', point_content_lower))
                trace_keywords = set(re.findall(r'\b\w+\b', all_trace_text))
                
                # Check for significant keyword overlap (at least 2 matching keywords, lowered from 3)
                keyword_overlap = len(point_keywords & trace_keywords)
                if keyword_overlap >= 2:
                    # Additional check: ensure the point is about something that happened
                    if self._point_matches_trace_activity(point, tool_names, all_trace_text):
                        used_point_ids.append(point.id)
                        heuristically_matched.add(point.id)
                        logger.debug(f"Point {point.id} matched via keyword overlap: {keyword_overlap} keywords")
                        continue
                
                # Method 3: Semantic matching for workflow/guidance points
                # Also apply to tool_usage points that mention generic commands (not specific tools)
                if point.category in ["workflow", "general"] or (
                    point.category == "tool_usage" and not self._extract_tool_name(point.content)
                ):
                    if self._semantic_match_point_to_trace(point, all_trace_text, reasoning_texts):
                        used_point_ids.append(point.id)
                        heuristically_matched.add(point.id)
                        logger.debug(f"Point {point.id} matched via semantic similarity")
                        continue
                
                # If not matched by heuristics, add to ambiguous list for LLM check
                ambiguous_points.append(point)
            
            # Second pass: LLM-based detection for ambiguous points (if enabled)
            if self.use_llm_usage_detection and ambiguous_points and trace_summary:
                llm_matched = self._llm_identify_used_points(ambiguous_points, trace_summary, agent_type)
                for point_id in llm_matched:
                    if point_id not in heuristically_matched:
                        used_point_ids.append(point_id)
                        logger.debug(f"Point {point_id} matched via LLM usage detection")
        
        heuristic_count = len(heuristically_matched) if 'heuristically_matched' in locals() else 0
        llm_count = len(used_point_ids) - heuristic_count
        mode = "LLM-primary" if (self.use_llm_primary and self.use_llm_usage_detection) else "heuristic-first"
        logger.info(
            f"Identified {len(used_point_ids)} used points out of {len(active_points)} active points "
            f"for {agent_type.value} ({heuristic_count} heuristic, {llm_count} LLM) [mode: {mode}]"
        )
        return used_point_ids
    
    def _is_tool_related_point(self, point: PromptPoint) -> bool:
        """Check if a point is tool-related (should use tool-level success)"""
        if point.category == "tool_usage":
            return True
        
        # Check if point mentions a specific tool
        tool_name = self._extract_tool_name(point.content)
        if tool_name:
            return True
        
        # Check for tool-related keywords
        point_lower = point.content.lower()
        tool_keywords = ["tool", "use", "call", "execute", "kubectl", "get_metrics", "get_traces", 
                        "get_services", "exec_kubectl", "submit_tool"]
        if any(keyword in point_lower for keyword in tool_keywords):
            return True
        
        return False
    
    def _point_matches_trace_activity(self, point: PromptPoint, tool_names: List[str], trace_text: str) -> bool:
        """Check if a point's content matches the actual activity in the trace"""
        point_lower = point.content.lower()
        
        # For tool-related points, check if the tool was actually called
        if "tool" in point_lower or any(tool in point_lower for tool in tool_names):
            return True
        
        # Check for action verbs that match trace activity
        action_verbs = ["check", "verify", "examine", "analyze", "use", "call", "execute", "run"]
        point_actions = [verb for verb in action_verbs if verb in point_lower]
        trace_actions = [verb for verb in action_verbs if verb in trace_text]
        
        if point_actions and any(action in trace_actions for action in point_actions):
            return True
        
        return False
    
    def _semantic_match_point_to_trace(self, point: PromptPoint, trace_text: str, reasoning_texts: List[str]) -> bool:
        """Check if a point semantically matches the trace using simple heuristics"""
        point_lower = point.content.lower()
        
        # Extract key concepts from point
        point_concepts = set(re.findall(r'\b\w{4,}\b', point_lower))  # Words 4+ chars
        
        # Extract key concepts from trace
        trace_concepts = set(re.findall(r'\b\w{4,}\b', trace_text))
        
        # Check for concept overlap
        concept_overlap = len(point_concepts & trace_concepts)
        total_point_concepts = len(point_concepts)
        
        if total_point_concepts == 0:
            return False
        
        # If 30%+ of point's concepts appear in trace, likely related
        overlap_ratio = concept_overlap / total_point_concepts
        if overlap_ratio >= 0.3:
            return True
        
        # Check reasoning steps for explicit mentions
        for reasoning in reasoning_texts:
            reasoning_lower = reasoning.lower()
            # Check if point's main instruction appears in reasoning
            point_sentences = [s.strip() for s in re.split(r'[.!?]', point.content) if len(s.strip()) > 10]
            for sentence in point_sentences[:2]:  # Check first 2 sentences
                sentence_keywords = set(re.findall(r'\b\w{3,}\b', sentence.lower()))
                if len(sentence_keywords) >= 3:
                    reasoning_keywords = set(re.findall(r'\b\w{3,}\b', reasoning_lower))
                    if len(sentence_keywords & reasoning_keywords) >= 2:
                        return True
        
        return False
    
    def _prepare_trace_summary(self, trace: Any, tool_names: List[str], reasoning_texts: List[str]) -> str:
        """Prepare a summary of the trace for LLM analysis"""
        summary_parts = []
        
        # Tool calls summary
        if tool_names:
            summary_parts.append(f"**Tools Used:** {', '.join(set(tool_names))}")
        else:
            # Even if no tool names, provide placeholder to ensure LLM detection can run
            summary_parts.append(f"**Tools Used:** (no tool calls recorded)")
        
        # Reasoning summary (first 3 reasoning steps, truncated)
        if reasoning_texts:
            reasoning_summary = " | ".join(reasoning_texts[:3])
            if len(reasoning_summary) > 500:
                reasoning_summary = reasoning_summary[:500] + "..."
            summary_parts.append(f"**Agent Reasoning:** {reasoning_summary}")
        else:
            # Provide placeholder if no reasoning
            summary_parts.append(f"**Agent Reasoning:** (no reasoning steps recorded)")
        
        # Final submission if available
        final_submission = getattr(trace, 'final_submission', None)
        if final_submission:
            if len(final_submission) > 200:
                final_submission = final_submission[:200] + "..."
            summary_parts.append(f"**Final Submission:** {final_submission}")
        
        # Success status
        success = getattr(trace, 'success', False)
        summary_parts.append(f"**Execution Result:** {'SUCCESS' if success else 'FAILED'}")
        
        # Always return non-empty string to ensure LLM detection can run
        summary = "\n".join(summary_parts)
        return summary if summary.strip() else "**Trace Summary:** (minimal trace data available)"
    
    def _llm_identify_used_points(self, points: List[PromptPoint], trace_summary: str, agent_type: AgentType) -> List[str]:
        """
        Use LLM to identify which points were actually used in the trace.
        
        Args:
            points: List of points to check
            trace_summary: Summary of the trace
            agent_type: Agent type for context
            
        Returns:
            List of point IDs that were used
        """
        llm_backend = self._get_llm_backend()
        if not llm_backend:
            logger.debug("LLM backend not available for usage detection, skipping")
            return []
        
        if not points:
            return []
        
        # Batch points for efficiency (check multiple points in one LLM call)
        # Split into batches of 5-10 points to avoid token limits
        batch_size = 8
        used_point_ids = []
        
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            # Rate limiting: ensure minimum delay between LLM calls
            self._rate_limit_llm_call()
            batch_results = self._llm_check_point_batch(batch, trace_summary, agent_type)
            used_point_ids.extend(batch_results)
        
        return used_point_ids
    
    def _rate_limit_llm_call(self):
        """Ensure minimum delay between LLM calls to avoid rate limiting"""
        current_time = time.time()
        time_since_last_call = current_time - self._last_llm_call_time
        if time_since_last_call < self._llm_call_delay:
            sleep_time = self._llm_call_delay - time_since_last_call
            logger.debug(f"Rate limiting: sleeping {sleep_time:.2f}s before next LLM call")
            time.sleep(sleep_time)
        self._last_llm_call_time = time.time()
    
    def _llm_check_point_batch(self, points: List[PromptPoint], trace_summary: str, agent_type: AgentType) -> List[str]:
        """Check a batch of points using LLM"""
        llm_backend = self._get_llm_backend()
        if not llm_backend:
            return []
        
        try:
            # Format points for LLM
            points_text = []
            for idx, point in enumerate(points):
                points_text.append(f"**Point {idx + 1} (ID: {point.id}):**\n{point.content}")
            
            points_section = "\n\n".join(points_text)
            
            prompt = f"""You are an expert at analyzing AI agent execution traces and prompt guidelines.

Analyze the following agent execution trace and determine which prompt points/instructions were actually **USED** or **FOLLOWED** during this execution.

**Agent Type:** {agent_type.value}

**Execution Trace:**
{trace_summary}

**Available Prompt Points:**
{points_section}

For each point, determine if it was:
1. **USED** - The agent's actions/reasoning indicate this instruction was followed
2. **NOT USED** - There's no evidence this instruction was followed

Consider:
- Tool calls that match point recommendations
- Reasoning/thinking that aligns with point guidance
- Actions that follow point instructions
- Workflow steps that match point descriptions

IMPORTANT:
- Only mark as USED if there's clear evidence the point was followed
- Points about general principles that weren't explicitly applied should be NOT USED
- Be conservative - only mark points with strong evidence of usage

Respond with ONLY a JSON object in this exact format:
{{
    "used_points": [
        {{"point_id": "point-id-1", "used": true, "evidence": "brief explanation"}},
        {{"point_id": "point-id-2", "used": false, "evidence": "brief explanation"}},
        ...
    ]
}}"""

            # Make LLM call with retry logic for rate limiting
            max_retries = 3
            retry_delay = 5.0  # Start with 5 seconds
            response = None
            
            for attempt in range(max_retries):
                try:
                    response = llm_backend.inference(messages=[prompt], system_prompt=None)
                    break  # Success, exit retry loop
                except Exception as e:
                    error_str = str(e).lower()
                    # Check if it's a rate limit error
                    if "rate" in error_str or "limit" in error_str or "429" in error_str:
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)  # Exponential backoff
                            logger.warning(f"Rate limited. Retrying in {wait_time:.1f}s... (Attempt {attempt + 1}/{max_retries})")
                            time.sleep(wait_time)
                            continue
                        else:
                            logger.error(f"Max retries exceeded. Unable to complete LLM usage detection request.")
                            return []
                    else:
                        # Not a rate limit error, re-raise
                        raise
            
            if response is None:
                logger.warning("LLM call failed after retries, skipping this batch")
                return []
            
            # Extract content from response
            if hasattr(response, 'content'):
                response_text = response.content
            elif isinstance(response, str):
                response_text = response
            else:
                response_text = str(response)
            
            # Parse JSON response with robust extraction
            result = self._extract_json_from_response(response_text)
            
            if result is None:
                # Try to find JSON array directly for used_points
                array_match = re.search(r'\[[^\]]*"point_id"[^\]]*\]', response_text, re.DOTALL)
                if array_match:
                    try:
                        result = {"used_points": json.loads(array_match.group())}
                    except json.JSONDecodeError:
                        logger.warning(f"Could not parse JSON array from LLM usage detection response")
                        logger.info(f"LLM response (first 1000 chars): {response_text[:1000]}")
                        return []
                else:
                    logger.warning(f"Could not extract JSON from LLM usage detection response")
                    logger.info(f"LLM response (first 1000 chars): {response_text[:1000]}")
                    return []
            
            used_points = result.get("used_points", [])
            used_point_ids = []
            
            logger.debug(f"LLM returned {len(used_points)} point evaluations")
            for item in used_points:
                if isinstance(item, dict):
                    point_id = item.get("point_id")
                    used = item.get("used", False)
                    evidence = item.get("evidence", "")
                    if used and point_id:
                        used_point_ids.append(point_id)
                        logger.info(f"✅ LLM identified point {point_id[:12]}... as USED: {evidence[:100]}")
                    else:
                        logger.debug(f"❌ LLM marked point {point_id[:12] if point_id else 'unknown'}... as NOT USED: {evidence[:100] if evidence else 'no evidence'}")
            
            if not used_point_ids:
                logger.debug(f"LLM found 0 used points out of {len(points)} checked")
            
            return used_point_ids
            
        except Exception as e:
            logger.warning(f"Error during LLM usage detection: {e}")
            if 'response_text' in locals():
                logger.debug(f"LLM response was: {response_text[:500]}")
            return []
    
    def validate_point(self, agent_type: AgentType, point_id: str, trace_success: bool):
        """Validate a point based on trace results"""
        point = next((p for p in self.points[agent_type] if p.id == point_id), None)
        if not point:
            return

        point.verification_count += 1
        if trace_success:
            point.success_count += 1
        else:
            point.failure_count += 1

        point.last_updated = datetime.now().isoformat()

        # Auto-verify after 3 successes
        if point.verification_count >= 3 and point.success_count >= 2:
            point.mark_verified()
        
        # Auto-remove if consistently failing
        if point.should_remove():
            point.active = False
            logger.info(f"❌ Point {point.id} deactivated due to poor performance")

        self._save_points(agent_type)
    
    def validate_points_from_trace(self, agent_type: AgentType, trace: Any, trace_success: bool) -> Dict[str, bool]:
        """
        Validate all points that were used in a trace.
        
        For tool-related points, uses tool-level success (whether the tool call succeeded).
        For other points, uses stage-level success (whether the overall stage succeeded).
        
        Args:
            agent_type: The agent type for this trace
            trace: AgentTrace object from trace_collector
            trace_success: Whether the trace execution was successful (stage-level)
            
        Returns:
            Dictionary mapping point IDs to validation results (True = validated, False = failed)
        """
        used_point_ids = self.identify_used_points(agent_type, trace)
        validation_results = {}
        
        # Extract tool calls and their success status from trace
        tool_calls = getattr(trace, 'tool_calls', []) or []
        tool_success_map = {}  # Map tool_name -> list of success booleans
        for tc in tool_calls:
            tool_name = None
            tool_success = False
            
            if hasattr(tc, 'tool_name') and hasattr(tc, 'success'):
                tool_name = tc.tool_name
                tool_success = tc.success
            elif isinstance(tc, dict):
                tool_name = tc.get('tool_name', '')
                tool_success = tc.get('success', False)
            
            if tool_name:
                if tool_name not in tool_success_map:
                    tool_success_map[tool_name] = []
                tool_success_map[tool_name].append(tool_success)
        
        # Determine success for each tool (at least one successful call = tool success)
        tool_success_final = {
            tool_name: any(successes) if successes else False
            for tool_name, successes in tool_success_map.items()
        }
        
        for point_id in used_point_ids:
            point = next((p for p in self.points[agent_type] if p.id == point_id), None)
            if point:
                old_verified = point.verified
                
                # Determine if this is a tool-related point
                is_tool_point = self._is_tool_related_point(point)
                
                if is_tool_point:
                    # For tool-related points, use tool-level success
                    tool_name = self._extract_tool_name(point.content)
                    if tool_name and tool_name in tool_success_final:
                        # Use tool-level success
                        point_success = tool_success_final[tool_name]
                        logger.debug(
                            f"Point {point_id[:8]}... validated using tool-level success: "
                            f"tool={tool_name}, success={point_success}"
                        )
                    elif tool_name:
                        # Tool mentioned but not called - mark as failed
                        point_success = False
                        logger.debug(
                            f"Point {point_id[:8]}... mentions tool {tool_name} but tool was not called - marking as failed"
                        )
                    else:
                        # Tool-related point but couldn't extract tool name - fall back to stage success
                        point_success = trace_success
                        logger.debug(
                            f"Point {point_id[:8]}... is tool-related but couldn't extract tool name - using stage success"
                        )
                else:
                    # For non-tool points, use stage-level success
                    point_success = trace_success
                    logger.debug(
                        f"Point {point_id[:8]}... validated using stage-level success: {point_success}"
                    )
                
                self.validate_point(agent_type, point_id, point_success)
                validation_results[point_id] = point_success
                
                # Log if point was newly verified
                if not old_verified and point.verified:
                    logger.info(f"✅ Point {point_id} verified after use in successful trace")
        
        if used_point_ids:
            tool_points = sum(1 for pid in used_point_ids 
                            if self._is_tool_related_point(
                                next((p for p in self.points[agent_type] if p.id == pid), None)
                            ))
            stage_points = len(used_point_ids) - tool_points
            
            logger.info(
                f"Validated {len(used_point_ids)} points for {agent_type.value}: "
                f"{sum(validation_results.values())} successful, "
                f"{len(validation_results) - sum(validation_results.values())} failed "
                f"({tool_points} tool-level, {stage_points} stage-level)"
            )
        else:
            # Log when no points were identified (helps debug why validation isn't happening)
            active_points = [p for p in self.points.get(agent_type, []) if p.active]
            if active_points:
                logger.info(
                    f"Validated 0 points for {agent_type.value}: "
                    f"no points were identified as used in this trace "
                    f"(out of {len(active_points)} active points available)"
                )
            else:
                logger.info(
                    f"Validated 0 points for {agent_type.value}: "
                    f"no active points available for validation"
                )
        
        return validation_results

