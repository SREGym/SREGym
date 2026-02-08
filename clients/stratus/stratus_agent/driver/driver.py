import os
import sys
from pathlib import Path

# for external call
sregym_core_path = Path(__file__).resolve().parents[4]
if str(sregym_core_path) not in sys.path:
    sys.path.insert(0, str(sregym_core_path))

import asyncio
import json
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

from logger import init_logger

init_logger()

import logging

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from clients.stratus.stratus_agent.diagnosis_agent import single_run_with_predefined_prompts as diagnosis_single_run
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

logger = logging.getLogger("all.stratus.driver")
logger.propagate = True
logger.setLevel(logging.DEBUG)


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m%d_%H%M")
    return formatted_datetime


def save_combined_trajectory(all_trajectories, problem_id, output_dir="."):
    """
    Save combined trajectory from all agent stages to a single JSONL file.

    Args:
        all_trajectories: List of dicts with 'stage' and 'events' keys
        problem_id: Problem identifier for filename
        output_dir: Directory to save the file
    """
    from pathlib import Path

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = get_current_datetime_formatted()
    trajectory_file = output_dir / f"{timestamp}_{problem_id}_stratus_agent_trajectory.jsonl"

    def serialize_message(message):
        """Convert a LangChain message to a serializable dict"""
        msg_dict = {
            "type": message.__class__.__name__,
            "content": message.content,
        }
        # Properly serialize tool calls
        if hasattr(message, "tool_calls") and message.tool_calls:
            serialized_tool_calls = []
            for tc in message.tool_calls:
                if isinstance(tc, dict):
                    serialized_tool_calls.append(tc)
                else:
                    # Convert object to dict
                    serialized_tool_calls.append(
                        {
                            "name": getattr(tc, "name", None),
                            "args": getattr(tc, "args", None),
                            "id": getattr(tc, "id", None),
                        }
                    )
            msg_dict["tool_calls"] = serialized_tool_calls

        # Properly serialize additional_kwargs
        if hasattr(message, "additional_kwargs") and message.additional_kwargs:
            # Convert to dict and handle non-serializable objects
            try:
                msg_dict["additional_kwargs"] = json.loads(json.dumps(message.additional_kwargs, default=str))
            except:
                msg_dict["additional_kwargs"] = str(message.additional_kwargs)

        return msg_dict

    try:
        with open(trajectory_file, "w", encoding="utf-8") as f:
            # Write metadata
            total_events = sum(len(traj.get("events", [])) for traj in all_trajectories)
            metadata = {
                "type": "metadata",
                "problem_id": problem_id,
                "timestamp": timestamp,
                "timestamp_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_stages": len(all_trajectories),
                "total_events": total_events,
            }
            f.write(json.dumps(metadata) + "\n")

            # Write each stage
            for stage_idx, stage_data in enumerate(all_trajectories):
                stage_name = stage_data.get("stage", "unknown")
                events = stage_data.get("events", [])

                # Write stage marker
                stage_marker = {
                    "type": "stage_start",
                    "stage": stage_name,
                    "num_events": len(events),
                }
                f.write(json.dumps(stage_marker) + "\n")

                # Write events for this stage
                for idx, event in enumerate(events):
                    try:
                        event_data = {
                            "type": "event",
                            "stage": stage_name,
                            "event_index": idx,
                            "num_steps": event.get("num_steps", 0),
                            "submitted": event.get("submitted", False),
                            "rollback_stack": event.get("rollback_stack", ""),
                        }

                        # Serialize messages
                        if "messages" in event and event["messages"]:
                            event_data["messages"] = [serialize_message(msg) for msg in event["messages"]]
                            event_data["last_message"] = serialize_message(event["messages"][-1])

                        f.write(json.dumps(event_data) + "\n")
                    except Exception as e:
                        logger.error(f"[Driver] Failed to serialize event {idx} in stage {stage_name}: {e}")
                        # Write a placeholder event to maintain continuity
                        error_event = {
                            "type": "event",
                            "stage": stage_name,
                            "event_index": idx,
                            "error": f"Failed to serialize: {str(e)}",
                        }
                        f.write(json.dumps(error_event) + "\n")

        logger.info(f"[Driver] Saved trajectory to {trajectory_file}")
        return trajectory_file
    except Exception as e:
        logger.error(f"[Driver] Failed to save trajectory: {e}", exc_info=True)
        return None


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


def get_benchmark_status():
    """
    Check the current status of the benchmark.
    Returns the status string (e.g., "diagnosis", "mitigation", "done") or "error" on failure.
    """
    try:
        # Construct the status URL from the benchmark API (not the MCP URL)
        # The status endpoint is at http://localhost:API_PORT/status
        api_port = os.getenv("API_PORT", "8000")
        status_url = f"http://localhost:{api_port}/status"

        response = requests.get(status_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data.get("stage", "error")
        else:
            logger.warning(f"Failed to get benchmark status: {response.status_code}")
            return "error"
    except Exception as e:
        logger.warning(f"Exception while getting benchmark status: {e}")
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


async def diagnosis_with_localization_task_main():
    """Run diagnosis task (formerly called localization)."""
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
    agent, last_state, graph_events = await diagnosis_single_run(first_run_initial_messages)
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
    logger.info(f"Finished diagnosis agent run, output dict: {agent_exec_stats}")
    return agent_exec_stats, last_state, graph_events


async def mitigation_task_main(diagnosis_summary):
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
                faults_info=diagnosis_summary,
                app_name=app_name,
                app_description=app_description,
                app_namespace=app_namespace,
            )
        ),
    ]
    start_time = time.perf_counter()
    logger.info(f"running in retry mode: [{mitigation_agent_retry_mode}]")
    # mitigation task in plain English:
    # Collect all graph events from all agents in this mitigation run
    all_graph_events = []

    if mitigation_agent_retry_mode == "none":
        # if the retry mode is none, just run mitigation agent once.
        agent, last_state, graph_events = await mitigation_agent_single_run(first_run_initial_messages)
        all_graph_events.extend([{"stage": "mitigation", "events": graph_events}])
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
        return agent_exec_stats, all_graph_events

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
            agent, last_state, graph_events = await mitigation_agent_single_run(first_run_initial_messages)
            all_graph_events.append({"stage": f"mitigation_attempt_{curr_attempt}", "events": graph_events})

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
            try:
                oracle_results = await validate_oracles(oracles)
                oracle_results_lst.append(str(oracle_results))
                logger.info(f"oracle results: {oracle_results}")
                has_succeeded = oracle_results[0] is True
            except Exception as e:
                logger.error(f"Oracle validation failed with error: {e}", exc_info=True)
                oracle_results = [False, []]
                oracle_results_lst.append(f"Oracle error: {str(e)}")
                has_succeeded = False
            if has_succeeded:
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
        return agent_exec_stats, all_graph_events
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
                agent, mitigation_agent_last_state, graph_events = await mitigation_agent_single_run(
                    first_run_initial_messages
                )
                all_graph_events.append({"stage": f"mitigation_attempt_{curr_attempt}", "events": graph_events})
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
                            faults_info=diagnosis_summary,
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
                agent, mitigation_agent_last_state, graph_events = await mitigation_agent_retry_run(
                    retry_run_initial_messages
                )
                all_graph_events.append({"stage": f"mitigation_attempt_{curr_attempt}", "events": graph_events})

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
            try:
                oracle_results = await validate_oracles(oracles)
                oracle_results_lst.append(str(oracle_results))
                has_succeeded = oracle_results[0]
            except Exception as e:
                logger.error(f"Oracle validation failed with error: {e}", exc_info=True)
                oracle_results = [False, []]
                oracle_results_lst.append(f"Oracle error: {str(e)}")
                has_succeeded = False
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
                logger.info(f"agent failed, should we retry? {'Yes!' if should_retry else 'No!'}")
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
                    rollback_agent, rollback_agent_last_state, rollback_graph_events = await rollback_agent_main()
                    all_graph_events.append(
                        {"stage": f"rollback_attempt_{curr_attempt}", "events": rollback_graph_events}
                    )
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
        return agent_exec_stats, all_graph_events


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
    # logger.info("*" * 25 + " Starting [diagnosis agent] for [NOOP detection] " + "*" * 25)
    # diagnosis_agent_exec_stats = await diagnosis_task_main()
    # agent_names.append("diagnosis_agent_noop")
    # agent_in_tokens.append(diagnosis_agent_exec_stats["input_tokens"])
    # agent_out_tokens.append(diagnosis_agent_exec_stats["output_tokens"])
    # agent_total_tokens.append(diagnosis_agent_exec_stats["total_tokens"])
    # agent_times.append(diagnosis_agent_exec_stats["time"])
    # agent_steps.append(diagnosis_agent_exec_stats["steps"])
    # agent_retry_attempts.append(diagnosis_agent_exec_stats["num_retry_attempts"])
    # agent_rollback_stack.append(diagnosis_agent_exec_stats["rollback_stack"])
    # agent_oracle_results.append(diagnosis_agent_exec_stats["oracle_results"])
    # logger.info("*" * 25 + " Finished [diagnosis agent] " + "*" * 25)
    # logger.info("sleeping for a minute for fault propagation")
    # await asyncio.sleep(60)

    # 1 for faulty diagnosis
    # logger.info("*" * 25 + " Starting [diagnosis agent] for [Faulty detection] " + "*" * 25)
    # diagnosis_agent_exec_stats = await diagnosis_task_main()
    # agent_names.append("diagnosis_agent_faulty")
    # agent_in_tokens.append(diagnosis_agent_exec_stats["input_tokens"])
    # agent_out_tokens.append(diagnosis_agent_exec_stats["output_tokens"])
    # agent_total_tokens.append(diagnosis_agent_exec_stats["total_tokens"])
    # agent_times.append(diagnosis_agent_exec_stats["time"])
    # agent_steps.append(diagnosis_agent_exec_stats["steps"])
    # agent_retry_attempts.append(diagnosis_agent_exec_stats["num_retry_attempts"])
    # agent_rollback_stack.append(diagnosis_agent_exec_stats["rollback_stack"])
    # agent_oracle_results.append(diagnosis_agent_exec_stats["oracle_results"])
    # logger.info("*" * 25 + " Finished [diagnosis agent] " + "*" * 25)

    # Collect all trajectories from this run
    all_trajectories = []

    # run diagnosis agent 1 time for diagnosis (formerly called localization)
    # here, running the file's main function should suffice
    logger.info("*" * 25 + " Starting [diagnosis agent] for [diagnosis] " + "*" * 25)
    diagnosis_agent_exec_stats, diagnosis_agent_last_state, diagnosis_graph_events = (
        await diagnosis_with_localization_task_main()
    )
    all_trajectories.append({"stage": "diagnosis", "events": diagnosis_graph_events})
    agent_names.append("diagnosis_agent")
    agent_in_tokens.append(diagnosis_agent_exec_stats["input_tokens"])
    agent_out_tokens.append(diagnosis_agent_exec_stats["output_tokens"])
    agent_total_tokens.append(diagnosis_agent_exec_stats["total_tokens"])
    agent_times.append(diagnosis_agent_exec_stats["time"])
    agent_steps.append(diagnosis_agent_exec_stats["steps"])
    agent_retry_attempts.append(diagnosis_agent_exec_stats["num_retry_attempts"])
    agent_rollback_stack.append(diagnosis_agent_exec_stats["rollback_stack"])
    agent_oracle_results.append(diagnosis_agent_exec_stats["oracle_results"])
    logger.info("*" * 25 + " Finished [diagnosis agent] " + "*" * 25)

    file_parent_dir = Path(__file__).resolve().parent.parent
    diagnosis_agent_config_path = file_parent_dir.parent / "configs" / "diagnosis_agent_config.yaml"
    diagnosis_agent_config = yaml.safe_load(open(diagnosis_agent_config_path, "r"))
    diagnosis_agent_prompt_path = file_parent_dir.parent / "configs" / diagnosis_agent_config["prompts_path"]
    diagnosis_agent_prompts = yaml.safe_load(open(diagnosis_agent_prompt_path, "r"))

    # Check if diagnosis prompts have the summary prompt, otherwise use a default key
    summary_prompt_key = (
        "diagnosis_summary_prompt"
        if "diagnosis_summary_prompt" in diagnosis_agent_prompts
        else "localization_summary_prompt"
    )
    diagnosis_fault_summary = generate_run_summary(
        diagnosis_agent_last_state, diagnosis_agent_prompts[summary_prompt_key]
    )

    # Check benchmark status before attempting to run mitigation
    # If the benchmark is already done (e.g., no mitigation oracle configured),
    # we should skip mitigation to avoid the race condition
    benchmark_status = get_benchmark_status()
    logger.info(f"Benchmark status after diagnosis: {benchmark_status}")

    if benchmark_status == "done":
        logger.info(
            "Benchmark is already in 'done' status. Skipping mitigation agent. "
            "This typically means the problem does not have a mitigation oracle configured."
        )
    elif benchmark_status == "mitigation":
        # run mitigation task 1 time for mitigation
        # it includes retry logics
        logger.info("*" * 25 + " Starting [mitigation agent] for [mitigation] " + "*" * 25)
        mitigation_agent_exec_stats, mitigation_graph_events = await mitigation_task_main(diagnosis_fault_summary)
        all_trajectories.extend(mitigation_graph_events)
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
    else:
        logger.warning(
            f"Unexpected benchmark status: {benchmark_status}. Expected 'mitigation' or 'done'. "
            "Skipping mitigation agent to be safe."
        )

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

    save_combined_trajectory(all_trajectories, current_problem)

    logger.info("*" * 25 + f" Finished Testing {current_problem} ! " + "*" * 25)
    logger.info("*" * 25 + f" Finished Testing {current_problem} ! " + "*" * 25)
    logger.info("*" * 25 + f" Finished Testing {current_problem} ! " + "*" * 25)


if __name__ == "__main__":
    asyncio.run(main())
