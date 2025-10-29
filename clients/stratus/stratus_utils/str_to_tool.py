"""Converts tools in str into tool objects"""

import os
import uuid

from fastmcp import Client
from fastmcp.client import SSETransport
from langchain_core.tools import BaseTool

from clients.stratus.stratus_utils.get_logger import get_logger
from clients.stratus.tools.jaeger_tools import get_operations, get_services, get_traces
from clients.stratus.tools.kubectl_tools import (
    ExecKubectlCmdSafely,
    ExecReadOnlyKubectlCmd,
    GetPreviousRollbackableCmd,
    RollbackCommand,
)
from clients.stratus.tools.prometheus_tools import get_metrics
from clients.stratus.tools.submit_tool import fake_submit_tool, rollback_submit_tool, submit_tool
from clients.stratus.tools.wait_tool import wait_tool

logger = get_logger()


def get_client():
    session_id = str(uuid.uuid4())
    transport = SSETransport(
        url=f"{os.environ['MCP_SERVER_URL']}/kubectl_mcp_tools/sse",
        headers={"sregym_ssid": session_id},
    )
    client = Client(transport)
    return client


def str_to_tool(tool_struct: dict[str, str]):
    if tool_struct["name"] == "get_traces":
        return get_traces
    elif tool_struct["name"] == "get_services":
        return get_services
    elif tool_struct["name"] == "get_operations":
        return get_operations
    elif tool_struct["name"] == "get_metrics":
        return get_metrics
    elif tool_struct["name"] == "submit_tool":
        return submit_tool
    elif tool_struct["name"] == "f_submit_tool":
        return fake_submit_tool
    elif tool_struct["name"] == "r_submit_tool":
        return rollback_submit_tool
    elif tool_struct["name"] == "wait_tool":
        return wait_tool
    elif tool_struct["name"] == "exec_read_only_kubectl_cmd":
        client = get_client()
        exec_read_only_kubectl_cmd = ExecReadOnlyKubectlCmd(client)
        return exec_read_only_kubectl_cmd
    elif tool_struct["name"] == "exec_kubectl_cmd_safely":
        client = get_client()
        exec_kubectl_cmd_safely = ExecKubectlCmdSafely(client)
        return exec_kubectl_cmd_safely
    elif tool_struct["name"] == "rollback_command":
        client = get_client()
        rollback_command = RollbackCommand(client)
        return rollback_command
    elif tool_struct["name"] == "get_previous_rollbackable_cmd":
        client = get_client()
        get_previous_rollbackable_cmd = GetPreviousRollbackableCmd(client)
        return get_previous_rollbackable_cmd
