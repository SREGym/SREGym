import logging
import subprocess  # nosec B404
import time
from pathlib import Path

import pytest
import yaml

from tests.kubectl_tool_tests.nl2kubectl_agent import NL2KubectlAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

KUBECTL_TOOL_TEST_DIR = Path(__file__).resolve().parent
PRECONDITIONS_FILE = KUBECTL_TOOL_TEST_DIR / "tests" / "preconditions.yaml"
PRECONDITION_SETTINGS = yaml.safe_load(open(PRECONDITIONS_FILE))

TEST_SETTINGS = [
    KUBECTL_TOOL_TEST_DIR / "tests" / "create_del_test.yaml",
    KUBECTL_TOOL_TEST_DIR / "tests" / "patch_test.yaml",
]

# in seconds
TIME_OUT = 30
WAIT_TIME_GAP = 3


def exec_shell_cmd(cmd: str):
    try:
        out = subprocess.run(cmd, shell=True, check=True, capture_output=True)  # nosec B602
        result = out.stdout.decode("utf-8")
        logger.info(f"Successfully running command: {cmd}\nresult:\n{result}")
        return result
    except subprocess.CalledProcessError as e:
        error_info = e.stderr.decode("utf-8")
        logger.info(f"Error running command: {cmd}\nError:\n{error_info}")
        raise e


def get_agent(is_mock):
    if is_mock:
        llm = None
    else:
        from llm_backend.init_backend import get_llm_backend_for_agent

        llm = get_llm_backend_for_agent()

    agent = NL2KubectlAgent(llm)
    agent.build_agent(mock=is_mock)
    return agent


def validate_condition(validate_cmd, validator):
    opt = exec_shell_cmd(validate_cmd)
    return eval(validator)


def set_up_preconditions(preconditions):
    for precondition in preconditions:
        condition_set = PRECONDITION_SETTINGS[precondition]
        if "preconditions" in condition_set:
            set_up_preconditions(condition_set["preconditions"])

        if "validate_cmd" in condition_set:
            fixed = False
            total_slept_time = 0
            while True:
                is_valid = validate_condition(condition_set["validate_cmd"], condition_set["validator"])
                if not is_valid and not fixed:
                    logger.info(f'Precondition "{precondition}" is not met. Setting up precondition...')
                    exec_shell_cmd(condition_set["fix"])
                    fixed = True
                elif not is_valid and fixed:
                    if total_slept_time > (TIME_OUT - 1e-2):
                        raise Exception(f"Fail to set up precondition (timeout): {precondition}")
                    to_sleep = min(WAIT_TIME_GAP, TIME_OUT - total_slept_time)
                    time.sleep(to_sleep)
                    total_slept_time += to_sleep
                else:
                    logger.info(f'Precondition "{precondition}" is met now.')
                    break


def running_test(test_rounds, agent):
    for one_round in test_rounds:
        logger.info(f"user_input for this round: {one_round['user_input']}")
        agent.graph_step(one_round["user_input"])
        if "validate_cmd" in one_round:
            total_slept_time = 0
            logger.info("Validating agent's tool call...")
            while True:
                is_valid = validate_condition(one_round["validate_cmd"], one_round["validator"])
                if not is_valid:
                    if total_slept_time > (TIME_OUT - 1e-2):
                        raise Exception(f"Fail to validate (timeout). user_input: {one_round['user_input']}")
                    to_sleep = min(WAIT_TIME_GAP, TIME_OUT - total_slept_time)
                    time.sleep(to_sleep)
                    total_slept_time += to_sleep
                else:
                    logger.info(f"Success validating. user_input: {one_round['user_input']}")
                    break


@pytest.mark.parametrize("test_campaign_file", TEST_SETTINGS)
class TestKubectlTools:
    def test_kubectl_tools_success(self, test_campaign_file: str):
        agent = get_agent(is_mock=True)
        logger.info(f"agent msg switch branch: {agent.test_tool_or_ai_response}")
        agent.test_campaign_setter(test_campaign_file)
        test_campaign = yaml.safe_load(open(test_campaign_file))

        set_up_preconditions(test_campaign["preconditions"])
        running_test(test_campaign["test"], agent)


if __name__ == "__main__":
    pytest.main(
        [
            "-v",  # Verbose output
            "--maxfail=1",  # Stop after first failure
            "-s",  # Show print output
            f"{__file__}::TestKubectlTools::test_kubectl_tools_success",  # Specific test
        ]
    )
