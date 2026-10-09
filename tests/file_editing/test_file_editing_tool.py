"""Original file-tool campaigns through the current Stratus tool interface."""

from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage, ToolMessage

from clients.stratus.tools.text_editing.file_manip import goto_line, open_file

FIXTURES = Path(__file__).resolve().parent
CAMPAIGNS = ["test_open_file_1.yaml", "test_open_file_2.yaml", "test_goto_line_1.yaml"]


@pytest.mark.parametrize("campaign_name", CAMPAIGNS)
def test_file_campaign_uses_current_tools_on_temporary_files(campaign_name, tmp_path):
    campaign = yaml.safe_load((FIXTURES / campaign_name).read_text())
    original = (FIXTURES / "example.txt").read_bytes()
    working_file = tmp_path / "tests/file_editing/example.txt"
    working_file.parent.mkdir(parents=True)
    working_file.write_bytes(original)
    state = {"messages": [], "curr_file": "", "curr_line": "", "workdir": str(tmp_path)}
    tools = {"open_file": open_file, "goto_line": goto_line}
    for index, instruction in enumerate(campaign["tool_calls"]):
        arguments = {key: value for key, value in instruction.items() if key != "name"}
        if "path" in arguments:
            arguments["path"] = str(tmp_path / arguments["path"])
        call = {"name": instruction["name"], "args": arguments, "id": f"file-campaign-{index}", "type": "tool_call"}
        state["messages"].append(AIMessage(content="", tool_calls=[call]))
        result = tools[call["name"]].invoke({**call, "args": {"state": state, **arguments}})
        state = result.update
        assert isinstance(state["messages"][-1], ToolMessage)
        assert state["messages"][-1].tool_call_id == call["id"]
    assert state["curr_file"] == str(tmp_path / campaign["expected_curr_file"])
    assert state["curr_line"] == str(campaign["expected_curr_line"])
    assert campaign["expected_output"] in state["messages"][-1].content
    assert working_file.read_bytes() == original
    assert (FIXTURES / "example.txt").read_bytes() == original
