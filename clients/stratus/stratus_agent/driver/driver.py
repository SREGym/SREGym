import asyncio
from pathlib import Path
from typing import List

import yaml

from clients.stratus.stratus_agent.diagnosis_agent import main as diagnosis_task_main
from clients.stratus.stratus_agent.localization_agent import main as localization_task_main
from clients.stratus.stratus_agent.mitigation_agent import (
    reflect_run,
)
from clients.stratus.stratus_agent.mitigation_agent import retry_run_with_feedback as mitigation_agent_retry_run
from clients.stratus.stratus_agent.mitigation_agent import (
    single_run_with_predefined_prompts as mitigation_agent_single_run,
)
from clients.stratus.stratus_agent.rollback_agent import main as rollback_agent_main
from clients.stratus.stratus_utils.get_logger import get_logger
from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult
from clients.stratus.weak_oracles.cluster_state_oracle import ClusterStateOracle

logger = get_logger()


def validate_oracles(oracles: List[BaseOracle]) -> List[bool | List[OracleResult]]:
    results = []
    attempt_failed = False
    for oracle in oracles:
        res: OracleResult = oracle.validate()
        if not res.success:
            attempt_failed = True
            results.append(res)
    if attempt_failed:
        return [False, results]
    return [True, results]


async def mitigation_task_main():
    # run rollback, reflect, and retry for mitigation and rollback agent
    # note: not implementing a `mitigation_task_main()` like other agents above for rollback, reflect, and retry is due to these considerations
    #   1. keep each agent's main() method only about running that specific agent's loop until agent's submission
    #   2. mitigation agent is special as when we refer to "mitigation" as a task for the Stratus agent, we refer to the
    #      rollback, reflect, retry pipeline, which uses rollback agent too. Implementing logic about rollback agent
    #      inside mitigation agent's method seems against good SE practice.

    # getting some configs
    file_parent_dir = Path(__file__).resolve().parent.parent
    mitigation_agent_config_path = file_parent_dir.parent / "configs" / "mitigation_agent_config.yaml"
    mitigation_agent_config = yaml.safe_load(open(mitigation_agent_config_path, "r"))
    mitigation_agent_max_step = mitigation_agent_config["max_step"]
    mitigation_agent_prompt_path = file_parent_dir.parent / "configs" / mitigation_agent_config["prompts_path"]
    mitigation_agent_max_retry_attempts = mitigation_agent_config["max_retry_attempts"]
    mitigation_agent_retry_mode = mitigation_agent_config["retry_mode"]

    rollback_agent_config_path = file_parent_dir.parent / "configs" / "rollback_agent_config.yaml"
    rollback_agent_config = yaml.safe_load(open(rollback_agent_config_path, "r"))
    rollback_agent_max_step = rollback_agent_config["max_step"]
    rollback_agent_prompt_path = file_parent_dir.parent / "configs" / rollback_agent_config["prompts_path"]

    # oracle
    cluster_state_oracle = ClusterStateOracle()
    oracles = [cluster_state_oracle]

    # mitigation task in plain English:
    if mitigation_agent_retry_mode == "none":
        # if the retry mode is none, just run mitigation agent once.
        await mitigation_agent_single_run()
    elif mitigation_agent_retry_mode == "naive":
        # if the retry mode is naive, run mitigation agent with retry but no rollback agent.
        curr_attempt = 0
        last_state = ""
        while curr_attempt < mitigation_agent_max_retry_attempts:
            last_state = await mitigation_agent_single_run()
            oracle_results = validate_oracles(oracles)
            if oracle_results[0] is True:
                # agent succeeds, let's finish here.
                break
            # otherwise, naively retry
            curr_attempt += 1
        return last_state
    elif mitigation_agent_retry_mode == "validate":
        # if the retry mode is validation, run mitigation agent with rollback and weak oracle.
        # each start of new agent trial, the agent should receive the last run's oracle results
        # and some reflections as input
        # TODO: need new function for trajectory reflection
        curr_attempt = 0
        mitigation_agent_last_state = ""
        rollback_agent_last_state = ""
        while curr_attempt < mitigation_agent_max_retry_attempts:
            if curr_attempt == 0:
                mitigation_agent_last_state = await mitigation_agent_single_run()
            else:
                mitigation_agent_last_state = await mitigation_agent_retry_run(reflect_run(mitigation_agent_last_state))
            oracle_results = validate_oracles(oracles)
            if oracle_results[0] is True:
                # agent succeeds, let's finish here.
                break
            # otherwise, rollback all changes
            # rollback agent is stateless and "best effort" idempotent, just rollback
            # memory is cleared in the retry_run() method, so the agent can start anew.
            rollback_agent_last_state = rollback_agent_main()
            curr_attempt += 1
        return mitigation_agent_last_state


async def main():
    # run diagnosis agent 2 times
    # here, running the file's main function should suffice.
    # 1 for noop diagnosis
    logger.info("*" * 25 + "Starting [diagnosis agent] for [NOOP detection]" + "*" * 25)
    await diagnosis_task_main()
    logger.info("*" * 25 + "Finished [diagnosis agent]" + "*" * 25)

    # 1 for faulty diagnosis
    logger.info("*" * 25 + "Starting [diagnosis agent] for [Faulty detection]" + "*" * 25)
    await diagnosis_task_main()
    logger.info("*" * 25 + "Finished [diagnosis agent]" + "*" * 25)

    # run localization agent 1 time for localization
    # (BTS it's just diagnosis agent with different prompts)
    # here, running the file's main function should suffice
    logger.info("*" * 25 + "Starting [localization agent] for [localization]" + "*" * 25)
    await localization_task_main()
    logger.info("*" * 25 + "Finished [localization agent]" + "*" * 25)

    # run mitigation task 1 time for mitigation
    # it includes retry logics
    logger.info("*" * 25 + "Starting [mitigation agent] for [mitigation]" + "*" * 25)
    await mitigation_task_main()
    logger.info("*" * 25 + "Finished [mitigation agent]" + "*" * 25)


if __name__ == "__main__":
    asyncio.run(main())
