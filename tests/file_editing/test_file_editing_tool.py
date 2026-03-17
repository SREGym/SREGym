import os
import subprocess

import pytest
import yaml

from clients.test_k8s_agent.k8s_agent import XAgent
from llm_backend.init_backend import get_llm_backend_for_agent


def get_agent():
    llm = get_llm_backend_for_agent()
    xagent = XAgent(llm)
    xagent.build_agent(mock=True)
    return xagent


def feed_input_to_agent(xagent: XAgent, input_text: list[str]):
    for user_input in input_text:
        if user_input.lower() in ["quit", "exit", "q"]:
            print("Goodbye!")
            break
        xagent.graph_step(user_input)


ROOT_REPO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def git_restore_repo():
    """Restores all tracked files in the root of the repository."""
    try:
        subprocess.run(["git", "restore", "."], cwd=ROOT_REPO_PATH, check=True)
        print("Repository restored successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Failed to restore repository: {e}")


USER_INPUTS = [
    f"{ROOT_REPO_PATH}/tests/file_editing/test_open_file_1.yaml",
    f"{ROOT_REPO_PATH}/tests/file_editing/test_open_file_2.yaml",
    f"{ROOT_REPO_PATH}/tests/file_editing/test_goto_line_1.yaml",
]


@pytest.mark.parametrize("test_campaign_file", USER_INPUTS)
class TestOpenFile:
    def test_open_file_success(self, test_campaign_file: str):
        xagent = get_agent()
        print(f"xagent msg switch branch: {xagent.test_tool_or_ai_response}")
        xagent.test_campaign_setter(test_campaign_file)
        test_campaign = yaml.safe_load(open(test_campaign_file))
        print(f"test campaign: {test_campaign}")
        test_user_inputs = test_campaign["user_inputs"]
        feed_input_to_agent(xagent, test_user_inputs)
        config = {"configurable": {"thread_id": "1"}}
        assert (
            xagent.graph.get_state(config).values["curr_file"]
            == ROOT_REPO_PATH + "/" + test_campaign["expected_curr_file"]
        )
        assert xagent.graph.get_state(config).values["curr_line"] == str(test_campaign["expected_curr_line"])
        assert test_campaign["expected_output"] in xagent.graph.get_state(config).values["messages"][-2].content
        git_restore_repo()
