#!/usr/bin/env python3
"""
Run 5 Rounds of Learning with Accumulated Insights (SREGym Version)

This script runs the LLM learning test 5 times, accumulating insights across rounds.
Round 1 starts fresh with clean prompts and executes problems.
Rounds 2-5 execute problems and accumulate insights from previous rounds.
Each round is stored separately for comparison.

Adapted from SREArena for SREGym compatibility.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import re

import uvicorn
import yaml

# Import agent problem executor (isolated problem-solving logic)
from agent_problem_executor import run_problem, determine_execution_success, determine_stage_success, load_ground_truth

# Import MCP server components
from mcp_server.configs.load_all_cfg import mcp_server_cfg
from mcp_server.sregym_mcp_server import app as mcp_app
from mcp_tool_interceptor import enable_interception

# Configure logging first (before imports that might use logger)
logs_dir = Path("logs")
logs_dir.mkdir(exist_ok=True)
log_file = logs_dir / f"learning_5_rounds_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(str(log_file)),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logger.info(f"📝 Logging to: {log_file}")

# Import LLM meta-agent components (from SREArena, should be copied to SREGym)
try:
    from meta_agent.llm_meta_agent import LLMMetaAgent, LLMMetaAgentConfig
    from meta_agent.llm_optimizer import RewardSpec
    from meta_agent.trace_collector import AgentType, ProblemContext
except ImportError as e:
    logger.error(
        f"Failed to import meta_agent. Please copy the meta_agent directory from SREArena to SREGym. Error: {e}"
    )
    sys.exit(1)

# Import SREGym components
from sregym.conductor.conductor import Conductor
from sregym.conductor.conductor_api import request_shutdown, run_api, set_conductor
from sregym.conductor.constants import StartProblemResult


# load_ground_truth moved to agent_problem_executor.py


class AgentStageHandler(logging.Handler):
    """Custom logging handler to detect agent stage transitions"""

    def __init__(self, test_instance):
        super().__init__()
        self.test_instance = test_instance
        self.patterns = {
            r"Starting \[diagnosis agent\]": "diagnosis",
            r"Starting \[localization agent\]": "localization",
            r"Starting \[mitigation agent\]": "mitigation",
            r"running rollback agent": "rollback",
        }

    def emit(self, record):
        """Emit a log record and check for stage transitions"""
        if hasattr(self.test_instance, "set_agent_stage"):
            message = record.getMessage()
            for pattern, stage in self.patterns.items():
                if re.search(pattern, message):
                    self.test_instance.set_agent_stage(stage)
                    logger.info(f"🔀 Detected {stage} agent transition")


class MultiRoundLearningTest:
    """Multi-round learning test that accumulates insights across rounds"""

    def __init__(
        self,
        reward_spec: Optional[RewardSpec] = None,
        llm_model: str = "gemini/gemini-2.5-flash",
        delay_between_problems: int = 30,
        delay_between_rounds: int = 300,
        num_rounds: int = 5,
        start_round: int = 1,
        resume_from_round_path: Optional[str] = None,
    ):
        """Initialize multi-round learning test

        Args:
            reward_spec: Reward specification for LLM optimization
            llm_model: LLM model to use for optimization
            delay_between_problems: Delay between problems in seconds
            delay_between_rounds: Delay between rounds in seconds
            num_rounds: Number of learning rounds to run
        """

        # Create summary folder for all rounds
        self.summary_folder = Path(f"llm_learning_results/5_rounds_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        self.summary_folder.mkdir(parents=True, exist_ok=True)

        self.num_rounds = num_rounds
        self.start_round = start_round
        self.resume_from_round_path = resume_from_round_path
        self.delay_between_problems = delay_between_problems
        self.delay_between_rounds = delay_between_rounds
        self.llm_model = llm_model
        self.reward_spec = reward_spec or RewardSpec(
            success_weight=1.0,
            latency_weight=-0.5,
            attempts_weight=-0.3,
        )

        # Problem set (same as SREArena version)
        self.problem_set = [
            {
                "id": "social_net_hotel_res_astro_shop_concurrent_failures",
                "name": "Concurrent Failures - Social Network, Hotel Reservation, Astronomy Shop",
                "description": "Concurrent failures across multiple services: social network, hotel reservation, and astronomy shop",
            },
            {
                "id": "misconfig_app_hotel_res",
                "name": "Misconfiguration - Hotel Reservation App Mitigation",
                "description": "Application misconfiguration in hotel reservation service requiring mitigation",
            },
            {
                "id": "revoke_auth_mongodb-1",
                "name": "MongoDB Authentication Revocation",
                "description": "MongoDB authentication permissions revoked, causing connection failures",
            },
            {
                "id": "astronomy_shop_ad_service_high_cpu",
                "name": "High CPU Usage - Ad Service",
                "description": "Astronomy shop ad service experiencing high CPU usage causing performance degradation",
            },
            {
                "id": "valkey_memory_disruption",
                "name": "Memory Disruption - Valkey",
                "description": "Valkey memory disruption causing service instability",
            },
            {
                "id": "network_policy_block",
                "name": "Network Policy Block",
                "description": "Network policy blocking service communication between pods",
            },
            {
                "id": "duplicate_pvc_mounts_hotel_reservation",
                "name": "Storage Mount Issues - Hotel Reservation",
                "description": "Duplicate PVC mounts causing storage conflicts in hotel reservation app",
            },
        ]

        # Check for API key
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        self.api_key_available = bool(api_key)
        if not self.api_key_available:
            logger.warning(
                """
            ⚠️  No GOOGLE_API_KEY or GEMINI_API_KEY found in environment.
            LLM optimization will not work without this.
            Please set: export GOOGLE_API_KEY='your-api-key-here'
            """
            )
        else:
            logger.info("✅ API key found, LLM optimization enabled")

        # IMPORTANT: Clean all learned guidelines ONCE at the start of the experiment (only if starting from Round 1)
        if start_round == 1:
            logger.info("🧹 Cleaning all learned guidelines to start with original clean prompts...")
            config = LLMMetaAgentConfig(
                llm_model=llm_model,
                use_llm_optimization=self.api_key_available,
                optimize_prompts=True,
                optimize_configs=True,
                min_traces_for_llm_optimization=5,
                reward_spec=self.reward_spec,
            )
            temp_meta_agent = LLMMetaAgent(config)
            # Clear points that were loaded during initialization (cleaning phase shouldn't use them)
            if temp_meta_agent.guideline_generator.point_manager:
                from meta_agent.trace_collector import AgentType
                temp_meta_agent.guideline_generator.point_manager.points = {
                    agent_type: [] for agent_type in AgentType
                }
            clean_success = temp_meta_agent.clean_all_guidelines()
            if clean_success:
                logger.info("✅ Successfully cleaned all guidelines - experiment will start with fresh original prompts!")
            else:
                logger.warning("⚠️ Failed to clean guidelines - continuing with existing guidelines")
        else:
            logger.info(f"⏭️  Skipping cleaning (starting from Round {start_round})")

        # Round tracking
        self.all_rounds_info = []
        self.previous_round_prompts_path = resume_from_round_path

        logger.info(f"Initialized multi-round learning test with {num_rounds} rounds, {len(self.problem_set)} problems per round")

    def _find_latest_learned_prompts(self) -> Optional[Path]:
        """Find the latest run folder with learned prompts"""
        results_dir = Path("llm_learning_results")
        if not results_dir.exists():
            return None

        run_folders = [d for d in results_dir.iterdir() if d.is_dir() and d.name.startswith("run_")]
        if not run_folders:
            return None

        run_folders.sort(key=lambda x: x.stat().st_mtime, reverse=True)

        for run_folder in run_folders:
            prompts_folder = run_folder / "prompts"
            if prompts_folder.exists():
                prompt_files = list(prompts_folder.glob("*_v*.yaml")) + list(
                    prompts_folder.glob("*_agent_prompts*.yaml")
                )
                if prompt_files:
                    return prompts_folder

        return None

    def _load_learned_prompts(self, learned_prompts_path: str, meta_agent: LLMMetaAgent) -> None:
        """Load learned prompts from a specific path"""
        learned_path = Path(learned_prompts_path)

        if not learned_path.exists():
            logger.warning(f"⚠️ Learned prompts path does not exist: {learned_path}")
            return

        agent_files = {}
        for agent_type in [AgentType.DIAGNOSIS, AgentType.LOCALIZATION, AgentType.MITIGATION, AgentType.ROLLBACK]:
            optimized_file = learned_path / f"{agent_type.value}_v1.0.1.yaml"
            fallback_file = learned_path / f"{agent_type.value}_v1.0.0.yaml"
            active_file = learned_path / f"active_{agent_type.value}_agent_prompts.yaml"

            if optimized_file.exists():
                agent_files[agent_type] = optimized_file
            elif active_file.exists():
                agent_files[agent_type] = active_file
            elif fallback_file.exists():
                agent_files[agent_type] = fallback_file
            else:
                agent_files[agent_type] = None

        loaded_count = 0
        for agent_type, prompt_file in agent_files.items():
            if prompt_file and prompt_file.exists():
                try:
                    with open(prompt_file, "r") as f:
                        learned_prompt = yaml.safe_load(f)

                    if hasattr(meta_agent, "guideline_generator"):
                        self._extract_and_preserve_insights(agent_type, learned_prompt, meta_agent)
                        meta_agent.guideline_generator.prompt_templates[agent_type] = learned_prompt
                        loaded_count += 1
                        version = (
                            "v1.0.1 (optimized)"
                            if "v1.0.1" in prompt_file.name
                            else "v1.0.0" if "v1.0.0" in prompt_file.name else "active"
                        )
                        logger.info(
                            f"✅ Loaded learned prompt for {agent_type.value} ({version}) from {prompt_file.name}"
                        )

                except Exception as e:
                    logger.error(f"❌ Failed to load learned prompt for {agent_type.value}: {e}")

        logger.info(f"📚 Loaded {loaded_count}/4 learned prompts")

    def _extract_and_preserve_insights(self, agent_type: AgentType, loaded_prompt: Dict[str, Any], meta_agent: LLMMetaAgent) -> None:
        """Extract existing insights from loaded prompt and preserve them
        
        NOTE: When point-based system is enabled, we skip extracting insights from prompt text
        because points are loaded directly from JSON files and serve as the single source of truth.
        """
        if not hasattr(meta_agent, "guideline_generator"):
            return

        guideline_gen = meta_agent.guideline_generator

        # If using point-based system, skip old insight extraction
        # Points are loaded directly from JSON files and are the source of truth
        if guideline_gen.use_point_based and guideline_gen.point_manager:
            logger.debug(
                f"Skipping old insight extraction for {agent_type.value}: "
                f"using point-based system (points loaded from JSON files)"
            )
            # Still need to store original prompt (without insights section)
            base_prompt = loaded_prompt.copy()
            system_prompt = loaded_prompt.get("system", "")
            if "## Learned Insights" in system_prompt:
                parts = system_prompt.split("## Learned Insights", 1)
                base_prompt["system"] = parts[0].rstrip()
            
            if agent_type not in guideline_gen.original_prompts:
                import copy
                guideline_gen.original_prompts[agent_type] = copy.deepcopy(base_prompt)
            return

        # Traditional system: Extract insights from prompt text
        base_prompt = loaded_prompt.copy()
        insights_section_text = ""

        system_prompt = loaded_prompt.get("system", "")
        if "## Learned Insights" in system_prompt:
            parts = system_prompt.split("## Learned Insights", 1)
            base_system = parts[0].rstrip()
            insights_section_text = parts[1] if len(parts) > 1 else ""

            base_prompt["system"] = base_system

            insight_blocks = re.split(r'\n(✅ VERIFIED|⚠️ UNVERIFIED.*?)\n', insights_section_text)

            existing_insights = []
            i = 1
            while i < len(insight_blocks):
                if i + 1 < len(insight_blocks):
                    marker = insight_blocks[i]
                    content = insight_blocks[i + 1].strip() if i + 1 < len(insight_blocks) else ""

                    if content:
                        verified = "✅ VERIFIED" in marker
                        existing_insights.append({
                            "type": "add_guidance",
                            "content": content,
                            "pattern": "loaded from previous round",
                            "timestamp": datetime.now().isoformat(),
                            "verified": verified,
                            "verification_count": 3 if verified else 0,
                            "success_count": 3 if verified else 0,
                            "failure_count": 0,
                        })
                i += 2

            if existing_insights:
                guideline_gen.learned_insights[agent_type].extend(existing_insights)
                logger.info(f"📚 Preserved {len(existing_insights)} existing insights for {agent_type.value} from previous round (traditional format)")

        if agent_type not in guideline_gen.original_prompts:
            import copy
            guideline_gen.original_prompts[agent_type] = copy.deepcopy(base_prompt)

    async def run_single_round(self, round_number: int) -> Dict[str, Any]:
        """Run a single learning round"""
        logger.info(f"\n{'='*80}")
        if round_number == 1:
            logger.info(f"ROUND {round_number}/{self.num_rounds} - Starting Fresh with Clean Prompts")
        else:
            logger.info(f"ROUND {round_number}/{self.num_rounds} - Accumulating Insights from Previous Rounds")
        logger.info(f"{'='*80}\n")

        round_start_time = time.time()

        # Create unique run folder for this round
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_folder = Path(f"llm_learning_results/run_{run_id}")
        run_folder.mkdir(parents=True, exist_ok=True)

        traces_folder = run_folder / "traces"
        traces_folder.mkdir(exist_ok=True)
        prompts_folder = run_folder / "prompts"
        prompts_folder.mkdir(exist_ok=True)
        configs_folder = run_folder / "configs"
        configs_folder.mkdir(exist_ok=True)
        points_folder = run_folder / "points"
        points_folder.mkdir(exist_ok=True)

        logger.info(f"📁 Created run folder: {run_folder}")

        # For Round 2+ (or if resuming), copy learned points from previous round
        if round_number > 1 and self.previous_round_prompts_path:
            previous_round_folder = Path(self.previous_round_prompts_path).parent
            previous_points_folder = previous_round_folder / "points"
            if previous_points_folder.exists():
                logger.info(f"📚 Copying learned points from Round {round_number - 1}...")
                import shutil
                for points_file in previous_points_folder.glob("*_points.json"):
                    dest_file = points_folder / points_file.name
                    shutil.copy2(points_file, dest_file)
                    logger.info(f"   Copied {points_file.name} to current round")
            else:
                logger.info(f"   No points folder found in previous round (starting fresh)")

        # Initialize meta-agent with run-specific points storage
        config = LLMMetaAgentConfig(
            llm_model=self.llm_model,
            use_llm_optimization=self.api_key_available,
            optimize_prompts=True,
            optimize_configs=True,
            min_traces_for_llm_optimization=5,
            min_traces_for_analysis=5,
            reward_spec=self.reward_spec,
            points_storage_path=str(points_folder),  # Use run-specific folder for points
        )

        meta_agent = LLMMetaAgent(config)
        meta_agent.trace_collector.storage_dir = traces_folder

        # Load existing prompts if not Round 1 (or if resuming from a specific round)
        if round_number > 1 and self.previous_round_prompts_path:
            logger.info(f"📚 Loading accumulated prompts from Round {round_number - 1}...")
            logger.info(f"   Prompts path: {self.previous_round_prompts_path}")
            self._load_learned_prompts(self.previous_round_prompts_path, meta_agent)

        # Initialize conductor
        conductor = Conductor()
        conductor.register_agent("llm_learning_test")

        # Server processes
        api_thread = None
        mcp_thread = None

        # Add custom log handler
        stage_handler = AgentStageHandler(self)
        root_logger = logging.getLogger()
        root_logger.addHandler(stage_handler)

        # Round info
        round_info = {
            "round_number": round_number,
            "run_id": run_id,
            "run_folder": str(run_folder),
            "start_time": datetime.now().isoformat(),
        }

        # Trace tracking
        self.current_trace_ids = None
        self.current_agent_type = None
        self.meta_agent = meta_agent
        self.conductor = conductor

        try:
            # Setup servers
            if not await self.setup_servers(conductor):
                logger.error("Failed to setup servers")
                round_info["status"] = "failed"
                round_info["error"] = "Failed to setup servers"
                return round_info

            start_time = time.time()
            results = []

            # Run all problems
            for i, problem_info in enumerate(self.problem_set, 1):
                logger.info(f"\n[{i}/{len(self.problem_set)}] Processing problem...")

                result = await self.run_problem(problem_info, meta_agent, conductor)
                results.append(result)

                # Don't validate points immediately - defer until all problems finish
                # This reduces rate limiting issues and allows batch processing
                # self._update_insight_verification(result, meta_agent)

                # Save intermediate results
                self._save_round_results(run_folder, results, start_time)

                # Wait before next problem (except for last one)
                if i < len(self.problem_set):
                    logger.info(f"⏳ Waiting {self.delay_between_problems}s before next problem...")
                    await asyncio.sleep(self.delay_between_problems)

            total_duration = time.time() - start_time
            logger.info(f"\n{'='*60}")
            logger.info(f"All problems completed in {total_duration:.2f}s")
            logger.info(f"{'='*60}\n")

            # Validate all points from all traces (batch processing after all problems finish)
            logger.info(f"\n{'='*60}")
            logger.info("Validating points from all traces (batch processing)")
            logger.info(f"{'='*60}\n")
            self._validate_all_points_from_all_problems(results, meta_agent)

            # Run LLM optimization
            learning_result = self.run_llm_optimization(meta_agent)

            # Save prompts and configs
            self._save_prompts_and_configs(run_folder, meta_agent)

            # Save final results
            self._save_round_results(run_folder, results, start_time, learning_result)

            # Update round info
            round_info["end_time"] = datetime.now().isoformat()
            round_info["duration"] = time.time() - round_start_time
            round_info["status"] = "completed"
            round_info["results_file"] = str(run_folder / "learning_results.json")
            round_info["results"] = {
                "completed_problems": len(results),
                "successful_problems": sum(1 for r in results if r.get("success", False)),
                "total_duration": total_duration,
            }

            logger.info(f"\n✅ ROUND {round_number}/{self.num_rounds} completed successfully")
            logger.info(f"   Run ID: {run_id}")
            logger.info(f"   Duration: {round_info['duration']:.2f}s ({round_info['duration']/60:.2f} min)")
            logger.info(
                f"   Problems: {round_info['results']['successful_problems']}/{round_info['results']['completed_problems']} successful"
            )

            # Update prompts path for next round
            if prompts_folder.exists():
                self.previous_round_prompts_path = str(prompts_folder)
                logger.info(f"✅ Saved prompts path for Round {round_number + 1}: {self.previous_round_prompts_path}")

        except Exception as e:
            logger.error(f"\n❌ ROUND {round_number}/{self.num_rounds} failed: {e}")
            round_info["end_time"] = datetime.now().isoformat()
            round_info["duration"] = time.time() - round_start_time
            round_info["status"] = "failed"
            round_info["error"] = str(e)
        finally:
            self._stop_servers()
            root_logger.removeHandler(stage_handler)

        return round_info

    def set_agent_stage(self, stage: str):
        """Set current agent stage for trace tracking"""
        stage_map = {
            "diagnosis": AgentType.DIAGNOSIS,
            "localization": AgentType.LOCALIZATION,
            "mitigation": AgentType.MITIGATION,
            "rollback": AgentType.ROLLBACK,
        }
        self.current_agent_type = stage_map.get(stage)
        if self.current_trace_ids and self.current_agent_type:
            trace_id = self.current_trace_ids.get(self.current_agent_type)
            if trace_id:
                enable_interception(self.meta_agent, trace_id=trace_id)

    async def setup_servers(self, conductor: Conductor) -> bool:
        """Setup both conductor API and MCP server"""
        try:
            set_conductor(conductor)
            self.api_thread = threading.Thread(target=self._run_api_server, daemon=True, args=(conductor,))
            self.api_thread.start()
            time.sleep(2)
            logger.info("🚀 Conductor API server started on localhost:8000")

            self.mcp_thread = threading.Thread(target=self._run_mcp_server, daemon=True)
            self.mcp_thread.start()
            time.sleep(3)
            logger.info(f"🚀 MCP server started on port {mcp_server_cfg.mcp_server_port}")

            return True
        except Exception as e:
            logger.error(f"❌ Failed to setup servers: {e}")
            return False

    def _run_api_server(self, conductor: Conductor):
        """Run the API server"""
        try:
            run_api(conductor)
        except Exception as e:
            logger.error(f"❌ API server error: {e}")

    def _run_mcp_server(self):
        """Run the MCP server"""
        try:
            port = mcp_server_cfg.mcp_server_port
            host = "0.0.0.0" if mcp_server_cfg.expose_server else "127.0.0.1"
            uvicorn.run(mcp_app, host=host, port=port)
        except Exception as e:
            logger.error(f"❌ MCP server error: {e}")

    def _validate_all_points_from_all_problems(self, results: List[Dict[str, Any]], meta_agent: LLMMetaAgent) -> None:
        """Batch validate all points from all problems after they all finish
        
        This is more efficient than validating after each problem because:
        1. Reduces rate limiting issues by batching LLM calls
        2. Allows better control over rate limiting
        3. Processes all traces in one batch
        """
        logger.info(f"Batch validating points from {len(results)} problems...")
        
        total_validated = 0
        for i, problem_result in enumerate(results, 1):
            problem_id = problem_result.get("problem_id", f"problem_{i}")
            logger.info(f"[{i}/{len(results)}] Validating points for problem: {problem_id}")
            
            try:
                self._update_insight_verification(problem_result, meta_agent)
                total_validated += 1
            except Exception as e:
                logger.error(f"Failed to validate points for problem {problem_id}: {e}")
        
        logger.info(f"✅ Batch validation complete: {total_validated}/{len(results)} problems validated")

    def _update_insight_verification(self, problem_result: Dict[str, Any], meta_agent: LLMMetaAgent) -> None:
        """Update insight verification based on problem execution results

        Uses granular validation: identifies which points were actually used in each trace
        and validates only those points, rather than validating all points for an agent type.
        """
        if not hasattr(meta_agent, "guideline_generator"):
            return

        guideline_gen = meta_agent.guideline_generator
        conductor_results = problem_result.get("conductor_results", {})
        trace_ids_dict = problem_result.get("trace_ids", {})

        # If using point-based system, use granular validation
        if guideline_gen.use_point_based and guideline_gen.point_manager:
            self._update_insight_verification_granular(
                problem_result, meta_agent, guideline_gen, conductor_results, trace_ids_dict
            )
        else:
            # Fallback to traditional validation (all insights for agent type)
            self._update_insight_verification_traditional(
                guideline_gen, conductor_results
            )

    def _update_insight_verification_granular(
        self,
        problem_result: Dict[str, Any],
        meta_agent: LLMMetaAgent,
        guideline_gen,
        conductor_results: Dict[str, Any],
        trace_ids_dict: Dict[str, str]
    ) -> None:
        """Granular validation: validate only points that were actually used in traces"""
        point_manager = guideline_gen.point_manager
        agent_stage_mapping = {
            AgentType.DIAGNOSIS: "Detection",
            AgentType.LOCALIZATION: "Localization",
            AgentType.MITIGATION: "Mitigation",
            AgentType.ROLLBACK: "Mitigation",
        }

        # Convert trace_ids_dict keys back to AgentType enums
        trace_ids_by_agent = {}
        for agent_type in [AgentType.DIAGNOSIS, AgentType.LOCALIZATION, AgentType.MITIGATION, AgentType.ROLLBACK]:
            trace_id_key = agent_type.value
            if trace_id_key in trace_ids_dict:
                trace_ids_by_agent[agent_type] = trace_ids_dict[trace_id_key]

        # For each agent type, get the trace and validate used points
        for agent_type, trace_id in trace_ids_by_agent.items():
            # Get trace from trace collector
            trace = meta_agent.trace_collector.get_trace(trace_id)

            # If trace not in active traces, try to load from disk
            if not trace:
                traces = meta_agent.trace_collector.load_traces(agent_type=agent_type, limit=100)
                trace = next((t for t in traces if t.trace_id == trace_id), None)

            if trace:
                # Determine stage success for this agent
                stage = agent_stage_mapping[agent_type]
                stage_success = conductor_results.get(stage, {}).get("success", False)

                # Validate only points that were used in this trace
                validation_results = point_manager.validate_points_from_trace(
                    agent_type, trace, stage_success
                )

                logger.debug(
                    f"Granular validation for {agent_type.value}: "
                    f"{len(validation_results)} points validated, "
                    f"{sum(validation_results.values())} successful"
                )
            else:
                logger.warning(f"Trace {trace_id} not found for {agent_type.value}, skipping granular validation")
                # Fallback to traditional validation for this agent type
                stage = agent_stage_mapping[agent_type]
                stage_success = conductor_results.get(stage, {}).get("success", False)
                insights = guideline_gen.learned_insights.get(agent_type, [])
                for insight_index in range(len(insights)):
                    guideline_gen.update_insight_verification(agent_type, insight_index, stage_success)

    def _update_insight_verification_traditional(
        self, guideline_gen, conductor_results: Dict[str, Any]
    ) -> None:
        """Traditional validation: validate all insights for each agent type"""
        agent_stage_mapping = {
            AgentType.DIAGNOSIS: "Detection",
            AgentType.LOCALIZATION: "Localization",
            AgentType.MITIGATION: "Mitigation",
            AgentType.ROLLBACK: "Mitigation",
        }

        for agent_type in [AgentType.DIAGNOSIS, AgentType.LOCALIZATION, AgentType.MITIGATION, AgentType.ROLLBACK]:
            stage = agent_stage_mapping[agent_type]
            stage_success = conductor_results.get(stage, {}).get("success", False)

            insights = guideline_gen.learned_insights.get(agent_type, [])
            for insight_index in range(len(insights)):
                guideline_gen.update_insight_verification(agent_type, insight_index, stage_success)

    async def run_problem(self, problem_info: dict, meta_agent: LLMMetaAgent, conductor: Conductor) -> Dict[str, Any]:
        """Run a single problem and collect traces - delegates to agent_problem_executor for execution, handles trace collection separately"""
        from datetime import datetime
        from meta_agent.trace_collector import ProblemContext
        from mcp_tool_interceptor import disable_interception, enable_interception
        
        # Start trace for all agent types (meta-agent learning functionality)
        base_trace_id = f"llm_learning_{problem_info['id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000)}"
        trace_ids = {}
        
        problem_context = ProblemContext(
            problem_id=problem_info["id"],
            app_name="SREGym Test Application",
            app_namespace="test-namespace",
            app_description=problem_info.get("description", ""),
            fault_type=problem_info["id"],
        )
        
        for agent_type in [AgentType.DIAGNOSIS, AgentType.LOCALIZATION, AgentType.MITIGATION, AgentType.ROLLBACK]:
            trace_id = f"{base_trace_id}_{agent_type.value}"
            trace_ids[agent_type] = trace_id
            meta_agent.collect_agent_trace(trace_id, agent_type, problem_context)
        
        self.current_trace_ids = trace_ids
        self.current_agent_type = AgentType.DIAGNOSIS
        
        # Set up trace ID getter for interception
        def get_current_trace_id():
            if self.current_agent_type and self.current_trace_ids:
                return self.current_trace_ids.get(self.current_agent_type)
            return trace_ids.get(AgentType.DIAGNOSIS)
        
        enable_interception(meta_agent, trace_id_getter=get_current_trace_id)
        
        try:
            # Delegate to agent_problem_executor for core execution (no meta-agent dependencies)
            result = await run_problem(
                problem_info=problem_info,
                conductor=conductor,
            )
            
            # End traces with ground truth and oracle results (meta-agent learning functionality)
            ground_truth = load_ground_truth(problem_info["id"])
            conductor_results = result.get("conductor_results", {})
            
            for agent_type, trace_id in trace_ids.items():
                # Map agent type to stage name for success determination
                stage_mapping = {
                    AgentType.DIAGNOSIS: "Detection",
                    AgentType.LOCALIZATION: "Localization",
                    AgentType.MITIGATION: "Mitigation",
                    AgentType.ROLLBACK: "Mitigation",
                }
                stage_name = stage_mapping.get(agent_type, "Detection")
                stage_success = determine_stage_success(stage_name, conductor_results)
                
                meta_agent.end_agent_trace(
                    trace_id,
                    success=stage_success,
                    ground_truth=ground_truth,
                    oracle_results=conductor_results,
                )
            
            # Add trace_ids to result for learning system
            trace_ids_dict = {k.value if isinstance(k, AgentType) else str(k): v for k, v in trace_ids.items()}
            result["trace_ids"] = trace_ids_dict
            
        except Exception as e:
            # End traces with error if we have trace IDs
            if self.current_trace_ids:
                for agent_type, trace_id in self.current_trace_ids.items():
                    meta_agent.end_agent_trace(trace_id, success=False, final_submission=f"Error: {str(e)}")
            raise
        finally:
            disable_interception()
        
        return result

    def run_llm_optimization(self, meta_agent: LLMMetaAgent):
        """Run LLM optimization on collected traces"""
        logger.info(f"\n{'='*60}")
        logger.info("Running LLM Optimization")
        logger.info(f"{'='*60}\n")

        if not self.api_key_available:
            logger.error("❌ API key not available, skipping LLM optimization")
            return None

        traces = meta_agent.trace_collector.load_traces(include_historical=False)
        logger.info(f"📊 Found {len(traces)} traces for optimization")

        if len(traces) < meta_agent.llm_config.min_traces_for_analysis:
            logger.warning(
                f"⚠️ Not enough traces. Need {meta_agent.llm_config.min_traces_for_analysis}, have {len(traces)}"
            )
            return None

        try:
            learning_result = meta_agent.start_learning_cycle()
            logger.info(f"✅ Learning cycle completed: {learning_result}")
            return learning_result
        except Exception as e:
            logger.error(f"❌ Learning cycle failed: {e}")
            return {"status": "error", "error": str(e)}

    def _convert_enum_keys(self, obj):
        """Recursively convert enum keys to strings for JSON serialization"""
        if isinstance(obj, dict):
            return {k.value if isinstance(k, AgentType) else str(k): self._convert_enum_keys(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_enum_keys(item) for item in obj]
        elif isinstance(obj, AgentType):
            return obj.value
        else:
            return obj

    def _save_round_results(self, run_folder: Path, results: List[Dict], start_time: float, learning_result: Optional[Dict] = None):
        """Save results to JSON file"""
        results_file = run_folder / "learning_results.json"

        output = {
            "run_id": run_folder.name.split("_")[-1] if "_" in run_folder.name else run_folder.name,
            "start_time": datetime.fromtimestamp(start_time).isoformat() if start_time else None,
            "total_duration": time.time() - start_time if start_time else 0,
            "problem_count": len(self.problem_set),
            "completed_problems": len(results),
            "results": self._convert_enum_keys(results),
            "reward_spec": {
                "success_weight": self.reward_spec.success_weight,
                "latency_weight": self.reward_spec.latency_weight,
                "attempts_weight": self.reward_spec.attempts_weight,
            },
            "learning_result": self._convert_enum_keys(learning_result) if learning_result else None,
        }

        with open(results_file, "w") as f:
            json.dump(output, f, indent=2, default=str)

        logger.info(f"💾 Results saved to: {results_file}")

    def _save_prompts_and_configs(self, run_folder: Path, meta_agent: LLMMetaAgent):
        """Save learned prompts and configs to run folder"""
        try:
            prompts_folder = run_folder / "prompts"
            configs_folder = run_folder / "configs"

            # Save current active prompts
            if hasattr(meta_agent, "guideline_generator"):
                for agent_type in [AgentType.DIAGNOSIS, AgentType.LOCALIZATION, AgentType.MITIGATION, AgentType.ROLLBACK]:
                    prompt_template = meta_agent.guideline_generator.prompt_templates.get(agent_type)
                    if prompt_template:
                        prompt_file = prompts_folder / f"active_{agent_type.value}_agent_prompts.yaml"
                        with open(prompt_file, "w") as f:
                            yaml.dump(prompt_template, f, default_flow_style=False, allow_unicode=True)

            # Save versioned prompts if they exist
            version_dir = Path("meta_agent/versions")
            if version_dir.exists():
                for version_file in version_dir.glob("*_v*.yaml"):
                    shutil.copy2(version_file, prompts_folder / version_file.name)

            # Save config files
            config_dir = Path("clients/stratus/configs")
            for config_file in config_dir.glob("*_agent_config.yaml"):
                shutil.copy2(config_file, configs_folder / config_file.name)

            logger.info(f"✅ Saved prompts and configs to run folder")

        except Exception as e:
            logger.warning(f"⚠️ Failed to save prompts/configs: {e}")

    def _stop_servers(self):
        """Stop all servers"""
        try:
            request_shutdown()
            logger.info("🛑 Servers stopped")
        except Exception as e:
            logger.error(f"❌ Error stopping servers: {e}")

    async def run_all_rounds(self):
        """Run all learning rounds"""
        logger.info("🚀 Starting 5 Rounds of Learning with Accumulated Insights")
        logger.info("=" * 80)
        logger.info(f"Configuration:")
        logger.info(f"  - LLM Model: {self.llm_model}")
        logger.info(f"  - Delay between problems: {self.delay_between_problems}s")
        logger.info(f"  - Delay between rounds: {self.delay_between_rounds}s")
        logger.info(f"  - Insight Accumulation: ENABLED (insights build across rounds)")
        logger.info(f"  - Round 1: Will execute problems fresh with clean prompts")
        logger.info(f"  - Success weight: {self.reward_spec.success_weight}")
        logger.info(f"  - Latency weight: {self.reward_spec.latency_weight}")
        logger.info(f"  - Attempts weight: {self.reward_spec.attempts_weight}")
        logger.info("=" * 80)

        overall_start_time = time.time()

        # Run all rounds starting from start_round
        for round_num in range(self.start_round, self.num_rounds + 1):
            round_info = await self.run_single_round(round_num)
            self.all_rounds_info.append(round_info)

            # Save round info immediately
            round_info_file = self.summary_folder / f"round_{round_num}_info.json"
            with open(round_info_file, "w") as f:
                json.dump(round_info, f, indent=2)

            # Wait between rounds (except after last round)
            if round_num < self.num_rounds:
                logger.info(f"\n⏳ Waiting {self.delay_between_rounds}s before next round...")
                await asyncio.sleep(self.delay_between_rounds)

        # Create summary
        overall_duration = time.time() - overall_start_time

        summary = {
            "experiment_info": {
                "start_time": datetime.fromtimestamp(overall_start_time).isoformat(),
                "end_time": datetime.now().isoformat(),
                "total_duration": overall_duration,
                "total_duration_minutes": overall_duration / 60,
                "llm_model": self.llm_model,
                "delay_between_problems": self.delay_between_problems,
                "delay_between_rounds": self.delay_between_rounds,
            },
            "reward_spec": {
                "success_weight": self.reward_spec.success_weight,
                "latency_weight": self.reward_spec.latency_weight,
                "attempts_weight": self.reward_spec.attempts_weight,
            },
            "rounds": self.all_rounds_info,
            "summary_statistics": {
                "total_rounds": len(self.all_rounds_info),
                "completed_rounds": sum(1 for r in self.all_rounds_info if r.get("status") == "completed"),
                "failed_rounds": sum(1 for r in self.all_rounds_info if r.get("status") == "failed"),
                "total_duration": overall_duration,
                "average_round_duration": (
                    sum(r.get("duration", 0) for r in self.all_rounds_info) / len(self.all_rounds_info)
                    if self.all_rounds_info
                    else 0
                ),
            },
        }

        # Save summary
        summary_file = self.summary_folder / "summary.json"
        with open(summary_file, "w") as f:
            json.dump(summary, f, indent=2)

        # Print final summary
        logger.info(f"\n{'='*80}")
        logger.info("5 ROUNDS LEARNING COMPLETE")
        logger.info(f"{'='*80}")
        logger.info(f"Total Duration: {overall_duration/60:.2f} minutes ({overall_duration:.2f}s)")
        logger.info(f"Completed Rounds: {summary['summary_statistics']['completed_rounds']}/{self.num_rounds}")
        logger.info(f"Failed Rounds: {summary['summary_statistics']['failed_rounds']}/{self.num_rounds}")
        logger.info(f"Average Round Duration: {summary['summary_statistics']['average_round_duration']/60:.2f} minutes")
        logger.info(f"\nSummary saved to: {summary_file}")
        logger.info(f"Individual round info saved in: {self.summary_folder}")
        logger.info(f"{'='*80}\n")

        # Print round-by-round summary
        logger.info("Round-by-Round Summary:")
        logger.info("-" * 80)
        for round_info in self.all_rounds_info:
            status_icon = "✅" if round_info.get("status") == "completed" else "❌"
            logger.info(
                f"{status_icon} Round {round_info['round_number']}: "
                f"{round_info.get('run_id', 'N/A')} - "
                f"{round_info.get('duration', 0)/60:.2f} min"
            )
            if "results" in round_info:
                logger.info(
                    f"   └─ Problems: {round_info['results']['successful_problems']}/"
                    f"{round_info['results']['completed_problems']} successful"
                )

        return summary


async def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Run 5 rounds of LLM learning with accumulated insights")
    parser.add_argument("--delay", type=int, default=30, help="Delay between problems (seconds)")
    parser.add_argument("--delay-between-rounds", type=int, default=300, help="Delay between rounds (seconds)")
    parser.add_argument("--model", type=str, default="gemini/gemini-2.5-flash", help="LLM model to use")
    parser.add_argument("--success-weight", type=float, default=1.0, help="Weight for success rate optimization")
    parser.add_argument("--latency-weight", type=float, default=-0.5, help="Weight for latency optimization (negative)")
    parser.add_argument(
        "--attempts-weight", type=float, default=-0.3, help="Weight for attempts optimization (negative)"
    )
    parser.add_argument("--num-rounds", type=int, default=5, help="Number of learning rounds to run")
    parser.add_argument("--start-round", type=int, default=1, help="Round number to start from (1-based)")
    parser.add_argument("--resume-from-round", type=str, default=None, help="Path to previous round's prompts folder to resume from (e.g., llm_learning_results/run_20251202_010401/prompts)")

    args = parser.parse_args()

    reward_spec = RewardSpec(
        success_weight=args.success_weight,
        latency_weight=args.latency_weight,
        attempts_weight=args.attempts_weight,
    )

    # Create and run multi-round test
    test = MultiRoundLearningTest(
        reward_spec=reward_spec,
        llm_model=args.model,
        delay_between_problems=args.delay,
        delay_between_rounds=args.delay_between_rounds,
        num_rounds=args.num_rounds,
        start_round=args.start_round,
        resume_from_round_path=args.resume_from_round,
    )
    
    # If resuming from a specific round, set the previous round prompts path
    if args.resume_from_round:
        test.previous_round_prompts_path = args.resume_from_round
        logger.info(f"📚 Resuming from Round {args.start_round} using prompts from: {args.resume_from_round}")

    await test.run_all_rounds()

    print("\n✅ Multi-round learning test completed!")
    print(f"\nResults saved to: {test.summary_folder}")


if __name__ == "__main__":
    asyncio.run(main())




