#!/usr/bin/env python3
"""
Agent Problem Executor

Isolated module for running agent problem solving.
This module handles the core execution logic for running a single problem
through the Stratus agent system without meta-agent dependencies.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from clients.stratus.stratus_agent.driver.driver import main as stratus_driver
from sregym.conductor.conductor import Conductor
from sregym.conductor.constants import StartProblemResult

logger = logging.getLogger(__name__)


def load_ground_truth(problem_id: str) -> Optional[Dict[str, Any]]:
    """Load ground truth for a problem from ground_truth_by_problem.json"""
    gt_file = Path("ground_truth_by_problem.json")
    if not gt_file.exists():
        logger.warning(f"Ground truth file not found: {gt_file}")
        return None

    try:
        with open(gt_file, "r") as f:
            all_gt = json.load(f)
        return all_gt.get(problem_id)
    except Exception as e:
        logger.warning(f"Failed to load ground truth for {problem_id}: {e}")
        return None


def determine_stage_success(stage_name: str, conductor_results: Dict[str, Any]) -> bool:
    """Determine stage-specific success for a given stage name"""
    # Ensure conductor_results is a dictionary
    if not isinstance(conductor_results, dict):
        logger.warning(f"conductor_results is not a dict (type: {type(conductor_results)}) in determine_stage_success")
        return False
    
    try:
        stage_result = conductor_results.get(stage_name, {})
        if isinstance(stage_result, dict):
            return stage_result.get("success", False)
        return False
    except Exception as e:
        logger.warning(f"Error determining stage success for {stage_name}: {e}")
        return False


def determine_execution_success(conductor: Conductor, conductor_results: Optional[Dict[str, Any]] = None) -> bool:
    """Determine if the execution was successful based on conductor results"""
    # Use provided conductor_results if available, otherwise get from conductor
    if conductor_results is None:
        if not hasattr(conductor, "results") or not conductor.results:
            logger.warning("No conductor results available")
            return False
        conductor_results = getattr(conductor, "results", {})
    
    # Ensure results is a dictionary (convert if needed)
    if not isinstance(conductor_results, dict):
        logger.warning(f"conductor.results is not a dict (type: {type(conductor_results)}), using empty dict")
        conductor_results = {}

    # Check for Diagnosis success (conductor stores as "Diagnosis", not "Detection")
    try:
        diagnosis_success = conductor_results.get("Diagnosis", {}).get("success", False) if isinstance(conductor_results.get("Diagnosis"), dict) else False
    except Exception as e:
        logger.warning(f"Error checking Diagnosis success: {e}")
        diagnosis_success = False

    # Also check for Detection (for backward compatibility with problems that use Detection stage)
    try:
        detection_success = conductor_results.get("Detection", {}).get("success", False) if isinstance(conductor_results.get("Detection"), dict) else False
    except Exception as e:
        logger.warning(f"Error checking Detection success: {e}")
        detection_success = False

    # Check for NOOP Detection
    try:
        noop_success = conductor_results.get("NOOP Detection", {}).get("success", False) if isinstance(conductor_results.get("NOOP Detection"), dict) else False
    except Exception as e:
        logger.warning(f"Error checking NOOP Detection success: {e}")
        noop_success = False

    return diagnosis_success or detection_success or noop_success


async def run_problem(
    problem_info: dict,
    conductor: Conductor,
) -> Dict[str, Any]:
    """
    Run a single problem through the Stratus agent system.
    
    Args:
        problem_info: Dictionary with problem information (id, name, description)
        conductor: Conductor instance for problem orchestration
    
    Returns:
        Dictionary with problem execution results including:
        - problem_id: Problem identifier
        - problem_name: Problem name
        - success: Whether the problem was solved successfully
        - duration: Execution duration in seconds
        - conductor_results: Results from conductor
        - stratus_result: Result from stratus driver
        - error: Error message if execution failed
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Running problem: {problem_info['name']} ({problem_info['id']})")
    logger.info(f"{'='*60}")

    problem_start = time.time()

    try:
        conductor.problem_id = problem_info["id"]
        result = await conductor.start_problem()
        
        # Check if problem was skipped
        if result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
            logger.warning(f"⏭️  Skipping problem '{problem_info['id']}': requires Khaos but running on emulated cluster")
            return {
                "problem_id": problem_info["id"],
                "problem_name": problem_info["name"],
                "success": False,
                "duration": 0,
                "error": "Skipped: Khaos required",
            }

        # Run the Stratus driver to solve the problem
        logger.info(f"Starting Stratus driver execution for {problem_info['id']}...")
        stratus_result = await stratus_driver()
        
        # Wait for grading to complete (following main.py pattern)
        while conductor.submission_stage != "done":
            await asyncio.sleep(1)

        execution_duration = time.time() - problem_start
        
        # Access conductor.results after submission_stage is "done" (following main.py pattern)
        # Add a small delay to ensure results are fully set
        await asyncio.sleep(0.5)
        conductor_results = getattr(conductor, "results", {})
        
        # Ensure conductor_results is a dictionary BEFORE using it (following main.py pattern which assumes it's a dict)
        # Handle case where results might be a CallToolResult or other non-dict type
        if not isinstance(conductor_results, dict):
            logger.warning(f"conductor.results is not a dict (type: {type(conductor_results)}), attempting to convert or using empty dict")
            # Try to convert if it has a dict-like interface, otherwise use empty dict
            try:
                if hasattr(conductor_results, '__dict__'):
                    conductor_results = conductor_results.__dict__
                elif hasattr(conductor_results, 'dict'):
                    conductor_results = conductor_results.dict()
                else:
                    conductor_results = {}
            except Exception as e:
                logger.warning(f"Failed to convert conductor.results to dict: {e}, using empty dict")
                conductor_results = {}
        
        # Now determine success with the converted dict
        success = determine_execution_success(conductor, conductor_results)

        problem_result = {
            "problem_id": problem_info["id"],
            "problem_name": problem_info["name"],
            "success": success,
            "duration": execution_duration,
            "stratus_result": stratus_result,
            "conductor_results": conductor_results,
        }

        # Log stage results similar to main.py
        logger.info(f"✅ Problem {problem_info['id']} completed: {'SUCCESS' if success else 'FAILED'}")
        if conductor_results:
            stage_summary = []
            for stage, outcome in conductor_results.items():
                if isinstance(outcome, dict):
                    stage_success = outcome.get("success", False)
                    stage_accuracy = outcome.get("accuracy", None)
                    if stage_accuracy is not None:
                        stage_summary.append(f"{stage}: success={stage_success}, accuracy={stage_accuracy:.2f}")
                    else:
                        stage_summary.append(f"{stage}: success={stage_success}")
                else:
                    stage_summary.append(f"{stage}: {outcome}")
            if stage_summary:
                logger.info(f"📊 Stage results: {', '.join(stage_summary)}")
        else:
            logger.warning(f"⚠️  No stage results found for problem {problem_info['id']}")
        return problem_result

    except Exception as e:
        import traceback
        logger.error(f"❌ Problem {problem_info['id']} failed: {e}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        execution_duration = time.time() - problem_start

        return {
            "problem_id": problem_info["id"],
            "problem_name": problem_info["name"],
            "success": False,
            "duration": execution_duration,
            "error": str(e),
        }

