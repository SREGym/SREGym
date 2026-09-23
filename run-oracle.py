"""
Script to run the mitigation oracle for a given SREGym problem.

This script assumes the application is already deployed and the fault is already injected.
It only runs the mitigation oracle to verify the system state.

Usage:
    python run-oracle.py --problem <problem_id>

Example:
    python run-oracle.py --problem incorrect_image
"""

import argparse
import logging
import sys
import time

from logger import init_logger
from sregym.conductor.problems.registry import ProblemRegistry

logger = logging.getLogger(__name__)


def run_oracle_for_problem(problem_id: str) -> dict:
    """
    Run the mitigation oracle for the specified problem.

    Args:
        problem_id: The ID of the problem to run the oracle for

    Returns:
        dict: Oracle evaluation result with at least {"success": bool}
    """
    # Initialize problem registry
    problem_registry = ProblemRegistry()

    # Verify problem exists
    problem_ids = problem_registry.get_problem_ids()
    if problem_id not in problem_ids:
        logger.error(f"Problem '{problem_id}' not found in registry.")
        logger.error(f"Available problems: {', '.join(sorted(problem_ids))}")
        sys.exit(1)

    # Get problem instance
    logger.info(f"Initializing problem: {problem_id}")
    problem = problem_registry.get_problem_instance(problem_id)

    # Check if mitigation oracle exists
    if not hasattr(problem, "mitigation_oracle") or problem.mitigation_oracle is None:
        logger.error(f"Problem '{problem_id}' does not have a mitigation oracle attached.")
        sys.exit(1)

    # Run the mitigation oracle
    logger.info("Running mitigation oracle...")
    try:
        result = problem.mitigation_oracle.evaluate()
        logger.info(f"Oracle evaluation complete: {result}")
        return result
    except Exception as e:
        logger.error(f"Error during oracle execution: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(
        description="Run the mitigation oracle for a given SREGym problem",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run-oracle.py --problem incorrect_image
  python run-oracle.py --problem scale_pod_zero_social_net
        """,
    )

    parser.add_argument("--problem", type=str, help="Problem ID to run the oracle for (e.g., 'incorrect_image')")

    args = parser.parse_args()

    # Initialize logging
    init_logger()

    # Run the oracle
    logger.info(f"Starting oracle execution for problem: {args.problem}")
    start_time = time.time()

    result = run_oracle_for_problem(args.problem)

    elapsed = time.time() - start_time
    logger.info(f"Oracle execution completed in {elapsed:.2f}s")

    # Print result summary
    print("\n" + "=" * 60)
    print("ORACLE RESULT")
    print("=" * 60)
    print(f"Problem ID: {args.problem}")
    print(f"Success: {result.get('success', False)}")
    print(f"Elapsed Time: {elapsed:.2f}s")
    if "error" in result:
        print(f"Error: {result['error']}")
    print("=" * 60)

    # Exit with appropriate code
    sys.exit(0 if result.get("success", False) else 1)


if __name__ == "__main__":
    main()
