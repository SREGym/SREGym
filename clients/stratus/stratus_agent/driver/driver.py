import asyncio
import time

# for parsing return values from benchmark app info as python dict
from ast import literal_eval
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd
import requests
import yaml
from langchain_core.messages import HumanMessage, SystemMessage

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from clients.stratus.stratus_agent.diagnosis_agent import single_run_with_predefined_prompts as diagnosis_single_run
from clients.stratus.stratus_agent.localization_agent import (
    single_run_with_predefined_prompts as localization_single_run,
)
from clients.stratus.stratus_agent.mitigation_agent import (
    generate_run_summary,
)
from clients.stratus.stratus_agent.mitigation_agent import retry_run_with_feedback as mitigation_agent_retry_run
from clients.stratus.stratus_agent.mitigation_agent import (
    single_run_with_predefined_prompts as mitigation_agent_single_run,
)
from clients.stratus.stratus_agent.rollback_agent import main as rollback_agent_main
from clients.stratus.stratus_utils.get_logger import get_logger
from clients.stratus.tools.submit_tool import manual_submit_tool
from clients.stratus.weak_oracles.base_oracle import BaseOracle, OracleResult
from clients.stratus.weak_oracles.cluster_state_oracle import ClusterStateOracle
from clients.stratus.weak_oracles.workload_oracle import WorkloadOracle
import logging
logger = logging.getLogger("all.stratus.driver")
logger.propagate = True
logger.setLevel(logging.DEBUG)


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m-%d_%H-%M")
    return formatted_datetime


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m-%d_%H-%M")
    return formatted_datetime


async def validate_oracles(oracles: List[BaseOracle]) -> List[bool | List[OracleResult]]:
    results = []
    attempt_failed = False
    for oracle in oracles:
        logger.info(f"validating oracle: {oracle}")
        res: OracleResult = await oracle.validate()
        if not res.success:
            attempt_failed = True
            results.append(res)
    if attempt_failed:
        return [False, results]
    return [True, results]


def get_app_info():
    ltc = LanggraphToolConfig()
    url = ltc.benchmark_app_info_url
    try:
        response = requests.get(url)
        logger.debug(f"Agent gets response: status: {response.status_code}, text: {response.text}")
        app_info_str = str(response.text)
        logger.debug(f"App info as str: {app_info_str} ")
        app_info = literal_eval(app_info_str)
        logger.debug(f"App info: {app_info}")
        return app_info
    except Exception as e:
        logger.error(f"[get_app_info] HTTP submission failed: {e}")
        return "error"


def get_curr_problem():
    ltc = LanggraphToolConfig()
    url = ltc.benchmark_current_problem
    try:
        response = requests.get(url)
        logger.info(f"Response status: {response.status_code}, text: {response.text}")
        problem_str = str(response.text)
        logger.info(f"problem as str: {problem_str}")
        problem = literal_eval(problem_str)
        logger.info(f"problem info: {problem}")
        return problem["problem_id"]
    except Exception as e:
        logger.error(f"[get_curr_problem] HTTP submission failed: {e}")
        return "error"


def get_app_class_by_name(app_name):
    target_app = ""
    if app_name == "Social Network":
        from sregym.service.apps.social_network import SocialNetwork

        target_app = SocialNetwork()
    elif app_name == "OpenTelemetry Demo Astronomy Shop":
        from sregym.service.apps.astronomy_shop import AstronomyShop

        target_app = AstronomyShop()
    elif app_name == "Flight Ticket":
        from sregym.service.apps.flight_ticket import FlightTicket

        logger.info(f"Flight ticket has never been tested!!")
        target_app = FlightTicket()
    elif app_name == "Hotel Reservation":
        from sregym.service.apps.hotel_reservation import HotelReservation

        target_app = HotelReservation()
    elif app_name == "TiDB Cluster with Operator":
        from sregym.service.apps.fleet_cast import FleetCast

        logger.info(f"TiDB has never been tested!!")
        target_app = FleetCast()
    elif app_name == "Train Ticket":
        from sregym.service.apps.train_ticket import TrainTicket

        target_app = TrainTicket()
    return target_app


async def diagnosis_task_main():
    logger.info("loading configs")
    file_parent_dir = Path(__file__).resolve().parent.parent
    diagnosis_agent_config_path = file_parent_dir.parent / "configs" / "diagnosis_agent_config.yaml"
    diagnosis_agent_config = yaml.safe_load(open(diagnosis_agent_config_path, "r"))
    diagnosis_agent_max_step = diagnosis_agent_config["max_step"]
    diagnosis_agent_prompt_path = file_parent_dir.parent / "configs" / diagnosis_agent_config["prompts_path"]
    diagnosis_agent_prompts = yaml.safe_load(open(diagnosis_agent_prompt_path, "r"))
    app_info = get_app_info()
    app_name = app_info["app_name"]
    app_description = app_info["descriptions"]
    app_namespace = app_info["namespace"]
    first_run_initial_messages = [
        SystemMessage(diagnosis_agent_prompts["system"]),
        HumanMessage(
            diagnosis_agent_prompts["user"].format(
                max_step=diagnosis_agent_max_step,
                app_name=app_name,
                app_description=app_description,
                app_namespace=app_namespace,
            )
        ),
    ]
    start_time = time.perf_counter()
    agent, last_state = await diagnosis_single_run(first_run_initial_messages)
    agent_time = time.perf_counter() - start_time
    agent_exec_stats = dict()
    # assuming we only use one model
    usage_metadata = next(iter(agent.callback.usage_metadata.items()))[1]
    logger.info(f"agent usage metadata: {usage_metadata}")
    agent_exec_stats["input_tokens"] = usage_metadata["input_tokens"]
    agent_exec_stats["output_tokens"] = usage_metadata["output_tokens"]
    agent_exec_stats["total_tokens"] = usage_metadata["total_tokens"]
    # assuming time in seconds.
    agent_exec_stats["time"] = str(agent_time)
    agent_exec_stats["steps"] = last_state.values["num_steps"]
    agent_exec_stats["num_retry_attempts"] = "N/A"
    agent_exec_stats["rollback_stack"] = "N/A"
    agent_exec_stats["oracle_results"] = "N/A"
    # agent_exec_stats["last_state"] = last_state
    logger.info(f"Finished diagnosis agent run, output dict: {agent_exec_stats}")
    return agent_exec_stats


async def localization_task_main():
    logger.info("loading configs")
    file_parent_dir = Path(__file__).resolve().parent.parent
    localization_agent_config_path = file_parent_dir.parent / "configs" / "localization_agent_config.yaml"
    localization_agent_config = yaml.safe_load(open(localization_agent_config_path, "r"))
    localization_agent_max_step = localization_agent_config["max_step"]
    localization_agent_prompt_path = file_parent_dir.parent / "configs" / localization_agent_config["prompts_path"]
    localization_agent_prompts = yaml.safe_load(open(localization_agent_prompt_path, "r"))
    app_info = get_app_info()
    app_name = app_info["app_name"]
    app_description = app_info["descriptions"]
    app_namespace = app_info["namespace"]
    first_run_initial_messages = [
        SystemMessage(localization_agent_prompts["system"]),
        HumanMessage(
            localization_agent_prompts["user"].format(
                max_step=localization_agent_max_step,
                app_name=app_name,
                app_description=app_description,
                app_namespace=app_namespace,
            )
        ),
    ]
    start_time = time.perf_counter()
    agent, last_state = await localization_single_run(first_run_initial_messages)
    agent_time = time.perf_counter() - start_time
    agent_exec_stats = dict()
    usage_metadata = next(iter(agent.callback.usage_metadata.items()))[1]
    agent_exec_stats["input_tokens"] = usage_metadata["input_tokens"]
    agent_exec_stats["output_tokens"] = usage_metadata["output_tokens"]
    agent_exec_stats["total_tokens"] = usage_metadata["total_tokens"]
    # assuming time in seconds.
    agent_exec_stats["time"] = str(agent_time)
    agent_exec_stats["steps"] = last_state.values["num_steps"]
    agent_exec_stats["num_retry_attempts"] = "N/A"
    agent_exec_stats["rollback_stack"] = "N/A"
    agent_exec_stats["oracle_results"] = "N/A"
    # agent_exec_stats["last_state"] = last_state
    logger.info(f"Finished localization agent run, output dict: {agent_exec_stats}")
    return agent_exec_stats, last_state


async def mitigation_task_main(localization_summary):
    # run rollback, reflect, and retry for mitigation and rollback agent
    # note: not implementing a `mitigation_task_main()` like other agents above for rollback, reflect, and retry is due to these considerations
    #   1. keep each agent's main() method only about running that specific agent's loop until agent's submission
    #   2. mitigation agent is special as when we refer to "mitigation" as a task for the Stratus agent, we refer to the
    #      rollback, reflect, retry pipeline, which uses rollback agent too. Implementing logic about rollback agent
    #      inside mitigation agent's method seems against good SE practice.

    # getting some configs
    logger.info("loading configs")
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

    llm_summarization_prompt_file = file_parent_dir.parent / "configs" / "llm_summarization_prompt.yaml"
    llm_summarization_prompt = yaml.safe_load(open(llm_summarization_prompt_file, "r"))["mitigation_retry_prompt"]
    mitigation_agent_prompts = yaml.safe_load(open(mitigation_agent_prompt_path, "r"))

    # oracle
    logger.info("setting up oracles")
    cluster_state_oracle = ClusterStateOracle()
    oracles = [cluster_state_oracle]

    # setting up workload oracle, need to interact with benchmark.
    logger.info("getting app info")
    app_info = get_app_info()
    app_name = app_info["app_name"]
    app_description = app_info["descriptions"]
    app_namespace = app_info["namespace"]
    target_app = get_app_class_by_name(app_name)
    if app_name not in ["Social Network", "Hotel Reservation"]:
        logger.info("Current app does not support workload oracle")
    else:
        logger.info(f"adding oracle for app [{app_name}]")
        workload_oracle = WorkloadOracle(target_app)
        oracles.append(workload_oracle)

    # defining the first set of messages that all retry mode share
    first_run_initial_messages = [
        SystemMessage(mitigation_agent_prompts["system"]),
        HumanMessage(
            mitigation_agent_prompts["user"].format(
                max_step=mitigation_agent_max_step,
                faults_info=localization_summary,
                app_name=app_name,
                app_description=app_description,
                app_namespace=app_namespace,
            )
        ),
    ]
    start_time = time.perf_counter()
    logger.info(f"running in retry mode: [{mitigation_agent_retry_mode}]")
    # mitigation task in plain English:
    if mitigation_agent_retry_mode == "none":
        # if the retry mode is none, just run mitigation agent once.
        agent, last_state = await mitigation_agent_single_run(first_run_initial_messages)
        agent_time = time.perf_counter() - start_time
        agent_exec_stats = dict()
        agent_exec_stats["agent_name"] = "mitigation_agent_none"
        usage_metadata = next(iter(agent.callback.usage_metadata.items()))[1]
        agent_exec_stats["input_tokens"] = usage_metadata["input_tokens"]
        agent_exec_stats["output_tokens"] = usage_metadata["output_tokens"]
        agent_exec_stats["total_tokens"] = usage_metadata["total_tokens"]
        # assuming time in seconds.
        agent_exec_stats["time"] = str(agent_time)
        agent_exec_stats["steps"] = last_state.values["num_steps"]
        agent_exec_stats["num_retry_attempts"] = "N/A"
        agent_exec_stats["rollback_stack"] = "N/A"
        agent_exec_stats["oracle_results"] = "N/A"
        # agent_exec_stats["last_state"] = last_state
        logger.info(f"Finished localization agent run, output dict: {agent_exec_stats}")
        return agent_exec_stats

    elif mitigation_agent_retry_mode == "naive":
        # if the retry mode is naive, run mitigation agent with retry but no rollback agent.
        curr_attempt = 0
        last_state = ""
        oracle_results = OracleResult(
            success=False, issues=["This is the beginning of mitigation, please observe the cluster for issues."]
        )
        agent_exec_stats = dict()
        agent_names_lst = []
        input_tokens_lst = []
        output_tokens_lst = []
        total_tokens_lst = []
        time_lst = []
        steps_lst = []
        num_retry_attempts_lst = []
        rollback_stack_lst = []
        oracle_results_lst = []
        while curr_attempt < mitigation_agent_max_retry_attempts:
            logger.info(f"current attempt: {curr_attempt + 1}/{mitigation_agent_max_retry_attempts}")
            agent, last_state = await mitigation_agent_single_run(first_run_initial_messages)

            # recording post-run data
            agent_time = time.perf_counter() - start_time
            agent_names_lst.append("mitigation_agent_naive")
            usage_metadata = next(iter(agent.callback.usage_metadata.items()))[1]
            input_tokens_lst.append(usage_metadata["input_tokens"])
            output_tokens_lst.append(usage_metadata["output_tokens"])
            total_tokens_lst.append(usage_metadata["total_tokens"])
            time_lst.append(str(agent_time))
            steps_lst.append(last_state.values["num_steps"])
            num_retry_attempts_lst.append(str(curr_attempt))
            rollback_stack_lst.append("N/A, naive retry")

            # getting oracle result
            oracle_results = await validate_oracles(oracles)
            oracle_results_lst.append(str(oracle_results))
            logger.info(f"oracle results: {oracle_results}")
            if oracle_results[0] is True:
                # agent succeeds, let's finish here.
                logger.info("agent succeeds, breaking!")
                break
            # otherwise, naively retry
            logger.info(f"agent failed, retrying... {curr_attempt + 1}/{mitigation_agent_max_retry_attempts}")
            curr_attempt += 1
        agent_exec_stats["agent_names"] = agent_names_lst
        agent_exec_stats["input_tokens"] = input_tokens_lst
        agent_exec_stats["output_tokens"] = output_tokens_lst
        agent_exec_stats["time"] = time_lst
        agent_exec_stats["total_tokens"] = total_tokens_lst
        agent_exec_stats["steps"] = steps_lst
        agent_exec_stats["num_retry_attempts"] = num_retry_attempts_lst
        agent_exec_stats["rollback_stack"] = rollback_stack_lst
        agent_exec_stats["oracle_results"] = oracle_results_lst
        return agent_exec_stats
    elif mitigation_agent_retry_mode == "validate":
        logger.info(f"retry mode: [{mitigation_agent_retry_mode}]")
        # if the retry mode is validation, run mitigation agent with rollback and weak oracle.
        # each start of new agent trial, the agent should receive the last run's oracle results
        # and some reflections as input
        curr_attempt = 0
        mitigation_agent_last_state = ""
        rollback_agent_last_state = ""
        oracle_results = OracleResult(
            success=False, issues=["This is the beginning of mitigation, please observe the cluster for issues."]
        )

        agent_exec_stats = dict()
        agent_names_lst = []
        input_tokens_lst = []
        output_tokens_lst = []
        total_tokens_lst = []
        time_lst = []
        steps_lst = []
        num_retry_attempts_lst = []
        rollback_stack_lst = []
        oracle_results_lst = []

        # starting retry loop
        while curr_attempt < mitigation_agent_max_retry_attempts:
            if curr_attempt == 0:
                logger.info(f"running first try")
                agent, mitigation_agent_last_state = await mitigation_agent_single_run(first_run_initial_messages)
            else:
                logger.info(
                    f"running retries. current attempt: {curr_attempt + 1}/{mitigation_agent_max_retry_attempts}"
                )
                # we compose the retry prompts here.
                last_run_summary = generate_run_summary(mitigation_agent_last_state, llm_summarization_prompt)
                retry_run_initial_messages = [
                    SystemMessage(mitigation_agent_prompts["system"]),
                    HumanMessage(
                        mitigation_agent_prompts["user"].format(
                            max_step=mitigation_agent_max_step,
                            faults_info=localization_summary,
                            app_name=app_name,
                            app_description=app_description,
                            app_namespace=app_namespace,
                        )
                        + "\n\n"
                        + mitigation_agent_prompts["retry_user"].format(
                            last_result=str(oracle_results),
                            reflection=last_run_summary,
                        )
                    ),
                ]
                logger.info(f"composed retry prompts: {retry_run_initial_messages}")
                agent, mitigation_agent_last_state = await mitigation_agent_retry_run(retry_run_initial_messages)

            # recording post-run data
            agent_time = time.perf_counter() - start_time
            agent_names_lst.append("mitigation_agent_validate")
            usage_metadata = next(iter(agent.callback.usage_metadata.items()))[1]
            input_tokens_lst.append(usage_metadata["input_tokens"])
            output_tokens_lst.append(usage_metadata["output_tokens"])
            total_tokens_lst.append(usage_metadata["total_tokens"])
            time_lst.append(str(agent_time))
            steps_lst.append(mitigation_agent_last_state.values["num_steps"])
            num_retry_attempts_lst.append(str(curr_attempt))
            rollback_stack_lst.append("N/A, mitigation agent")

            # getting oracle result
            oracle_results = await validate_oracles(oracles)
            oracle_results_lst.append(str(oracle_results))
            has_succeeded = oracle_results[0]
            if has_succeeded:
                # agent succeeds, let's finish here.
                logger.info("agent succeeds! manually submitting for the agent")
                await manual_submit_tool("")
                logger.info("breaking the retry loop")
                break
                # return agent_exec_stats
            else:
                # here the agent fails, we make decision if we should retry
                should_retry = curr_attempt + 1 < mitigation_agent_max_retry_attempts
                logger.info(f"agent failed, should we retry? {"Yes!" if should_retry else "No!"}")
                if should_retry:
                    # we should retry as we have more trials left
                    logger.info(
                        f"we should retry as we have more attempts left. attempts left: {(mitigation_agent_max_retry_attempts - 1) - (curr_attempt + 1)}"
                    )
                    # rollback all changes
                    # rollback agent is stateless and "best effort" idempotent, just rollback
                    # memory is cleared in the retry_run() method, so the agent can start anew.
                    logger.info(f"agent failed, retrying... {curr_attempt + 1}/{mitigation_agent_max_retry_attempts}")
                    logger.info(f"running rollback agent to reverse progress")
                    rollback_start_time = time.perf_counter()
                    rollback_agent, rollback_agent_last_state = await rollback_agent_main()
                    rollback_end_time = time.perf_counter() - rollback_start_time
                    agent_names_lst.append("rollback_agent")
                    usage_metadata = next(iter(rollback_agent.callback.usage_metadata.items()))[1]
                    input_tokens_lst.append(usage_metadata["input_tokens"])
                    output_tokens_lst.append(usage_metadata["output_tokens"])
                    total_tokens_lst.append(usage_metadata["total_tokens"])
                    time_lst.append(str(rollback_end_time))
                    steps_lst.append(rollback_agent_last_state.values["num_steps"])
                    num_retry_attempts_lst.append(str(curr_attempt))
                    rollback_stack_lst.append(rollback_agent_last_state.values["rollback_stack"])
                    oracle_results_lst.append(str("N/A, rollback agent"))
                    curr_attempt += 1
                else:
                    logger.info("we shouldn't retry as we don't have more attempts left.")
                    logger.info(f"making a real submission for the agent.")
                    await manual_submit_tool("")
                    break
                    # return agent_exec_stats

        agent_exec_stats["agent_name"] = agent_names_lst
        agent_exec_stats["input_tokens"] = input_tokens_lst
        agent_exec_stats["output_tokens"] = output_tokens_lst
        agent_exec_stats["total_tokens"] = total_tokens_lst
        agent_exec_stats["time"] = time_lst
        agent_exec_stats["steps"] = steps_lst
        agent_exec_stats["num_retry_attempts"] = num_retry_attempts_lst
        agent_exec_stats["rollback_stack"] = rollback_stack_lst
        agent_exec_stats["oracle_results"] = oracle_results_lst
        return agent_exec_stats


async def main():
    # run diagnosis agent 2 times
    # here, running the file's main function should suffice.
    # 1 for noop diagnosis
    current_problem = get_curr_problem()

    # logger.info("*" * 25 + f" Testing {current_problem} ! " + "*" * 25)
    # logger.info("*" * 25 + f" Testing {current_problem} ! " + "*" * 25)
    # logger.info("*" * 25 + f" Testing {current_problem} ! " + "*" * 25)
    agent_output_df = pd.DataFrame()
    agent_names = []
    agent_in_tokens = []
    agent_out_tokens = []
    agent_total_tokens = []
    agent_times = []
    agent_steps = []
    agent_retry_attempts = []
    agent_rollback_stack = []
    agent_oracle_results = []
    logger.info("*" * 25 + " Starting [diagnosis agent] for [NOOP detection] " + "*" * 25)
    diagnosis_agent_exec_stats = await diagnosis_task_main()
    agent_names.append("diagnosis_agent_noop")
    agent_in_tokens.append(diagnosis_agent_exec_stats["input_tokens"])
    agent_out_tokens.append(diagnosis_agent_exec_stats["output_tokens"])
    agent_total_tokens.append(diagnosis_agent_exec_stats["total_tokens"])
    agent_times.append(diagnosis_agent_exec_stats["time"])
    agent_steps.append(diagnosis_agent_exec_stats["steps"])
    agent_retry_attempts.append(diagnosis_agent_exec_stats["num_retry_attempts"])
    agent_rollback_stack.append(diagnosis_agent_exec_stats["rollback_stack"])
    agent_oracle_results.append(diagnosis_agent_exec_stats["oracle_results"])
    logger.info("*" * 25 + " Finished [diagnosis agent] " + "*" * 25)
    logger.info("sleeping for a minute for fault propagation")
    await asyncio.sleep(60)

    # 1 for faulty diagnosis
    logger.info("*" * 25 + " Starting [diagnosis agent] for [Faulty detection] " + "*" * 25)
    diagnosis_agent_exec_stats = await diagnosis_task_main()
    agent_names.append("diagnosis_agent_faulty")
    agent_in_tokens.append(diagnosis_agent_exec_stats["input_tokens"])
    agent_out_tokens.append(diagnosis_agent_exec_stats["output_tokens"])
    agent_total_tokens.append(diagnosis_agent_exec_stats["total_tokens"])
    agent_times.append(diagnosis_agent_exec_stats["time"])
    agent_steps.append(diagnosis_agent_exec_stats["steps"])
    agent_retry_attempts.append(diagnosis_agent_exec_stats["num_retry_attempts"])
    agent_rollback_stack.append(diagnosis_agent_exec_stats["rollback_stack"])
    agent_oracle_results.append(diagnosis_agent_exec_stats["oracle_results"])
    logger.info("*" * 25 + " Finished [diagnosis agent] " + "*" * 25)

    # run localization agent 1 time for localization
    # (BTS it's just diagnosis agent with different prompts)
    # here, running the file's main function should suffice
    logger.info("*" * 25 + " Starting [localization agent] for [localization] " + "*" * 25)
    localization_agent_exec_stats, localization_agent_last_state = await localization_task_main()
    agent_names.append("localization_agent")
    agent_in_tokens.append(localization_agent_exec_stats["input_tokens"])
    agent_out_tokens.append(localization_agent_exec_stats["output_tokens"])
    agent_total_tokens.append(localization_agent_exec_stats["total_tokens"])
    agent_times.append(localization_agent_exec_stats["time"])
    agent_steps.append(localization_agent_exec_stats["steps"])
    agent_retry_attempts.append(localization_agent_exec_stats["num_retry_attempts"])
    agent_rollback_stack.append(localization_agent_exec_stats["rollback_stack"])
    agent_oracle_results.append(localization_agent_exec_stats["oracle_results"])
    logger.info("*" * 25 + " Finished [localization agent] " + "*" * 25)

    file_parent_dir = Path(__file__).resolve().parent.parent
    localization_agent_config_path = file_parent_dir.parent / "configs" / "localization_agent_config.yaml"
    localization_agent_config = yaml.safe_load(open(localization_agent_config_path, "r"))
    localization_agent_prompt_path = file_parent_dir.parent / "configs" / localization_agent_config["prompts_path"]
    localization_agent_prompts = yaml.safe_load(open(localization_agent_prompt_path, "r"))
    localization_fault_summary = generate_run_summary(
        localization_agent_last_state, localization_agent_prompts["localization_summary_prompt"]
    )

    # run mitigation task 1 time for mitigation
    # it includes retry logics
    logger.info("*" * 25 + " Starting [mitigation agent] for [mitigation] " + "*" * 25)
    mitigation_agent_exec_stats = await mitigation_task_main(localization_fault_summary)
    agent_names.extend(mitigation_agent_exec_stats["agent_name"])
    agent_in_tokens.extend(mitigation_agent_exec_stats["input_tokens"])
    agent_out_tokens.extend(mitigation_agent_exec_stats["output_tokens"])
    agent_total_tokens.extend(mitigation_agent_exec_stats["total_tokens"])
    agent_times.extend(mitigation_agent_exec_stats["time"])
    agent_steps.extend(mitigation_agent_exec_stats["steps"])
    agent_retry_attempts.extend(mitigation_agent_exec_stats["num_retry_attempts"])
    agent_rollback_stack.extend(mitigation_agent_exec_stats["rollback_stack"])
    agent_oracle_results.extend(mitigation_agent_exec_stats["oracle_results"])
    logger.info("*" * 25 + " Finished [mitigation agent] " + "*" * 25)

    for lst in [
        agent_names,
        agent_in_tokens,
        agent_out_tokens,
        agent_total_tokens,
        agent_times,
        agent_steps,
        agent_retry_attempts,
        agent_rollback_stack,
        agent_oracle_results,
    ]:
        logger.info("list length: " + str(len(lst)))

    agent_output_df["agent_name"] = agent_names
    agent_output_df["input_tokens"] = agent_in_tokens
    agent_output_df["output_tokens"] = agent_out_tokens
    agent_output_df["total_tokens"] = agent_total_tokens
    agent_output_df["time"] = agent_times
    agent_output_df["steps"] = agent_steps
    agent_output_df["num_retry_attempts"] = agent_retry_attempts
    agent_output_df["rollback_stack"] = agent_rollback_stack
    agent_output_df["oracle_results"] = agent_oracle_results
    current_datetime = get_current_datetime_formatted()
    agent_output_df.to_csv(f"./{current_datetime}_{current_problem}_stratus_output.csv", index=False, header=True)
    logger.info("*" * 25 + f" Finished Testing {current_problem} ! " + "*" * 25)
    logger.info("*" * 25 + f" Finished Testing {current_problem} ! " + "*" * 25)
    logger.info("*" * 25 + f" Finished Testing {current_problem} ! " + "*" * 25)


if __name__ == "__main__":
    asyncio.run(main())
