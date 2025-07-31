from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, field_validator
import logging
import yaml
import os
import uuid
from pathlib import Path

from fastmcp.client.transports import SSETransport
from fastmcp import Client

from dotenv import load_dotenv
from clients.langgraph_agent.tools.kubectl_tools import \
    ExecKubectlCmdSafely, \
    RollbackCommand, \
    GetPreviousRollbackableCmd, \
    ExecReadOnlyKubectlCmd
from clients.langgraph_agent.tools.jaeger_tools import \
    get_traces, \
    get_services, \
    get_operations

from clients.langgraph_agent.tools.prometheus_tools import get_metrics
from clients.langgraph_agent.tools.submit_tool import submit_tool
from clients.langgraph_agent.tools.wait_tool import wait_tool

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

parent_dir = Path(__file__).resolve().parent


class BaseAgentCfg(BaseModel):
    max_round: int = Field(
        default=20,
        description="maximum rounds allowed for tool calling",
        gt=0
    )

    max_rec_round: int = Field(
        default=3,
        description="maximum rounds allowed for submission rectification",
        gt=0
    )

    max_tool_call_one_round: int = Field(
        default=5,
        description="maximum number of tool_calls allowed in one round",
        gt=0
    )

    prompts_file_path: str = Field(
        description="prompts used for diagnosis agent",
    )

    sync_tools: list[BaseTool] = Field(
        description="provided sync tools for the agent",
    )

    async_tools: list[BaseTool] = Field(
        description="provided async tools for the agent",
    )

    @field_validator("prompts_file_path")
    @classmethod
    def validate_prompts_file_path(cls, v):
        path = v
        if not os.path.exists(path):
            raise FileNotFoundError(f"Path does not exist: {path}")

        if not os.path.isfile(path):
            raise ValueError(f"Path is not a file: {path}")

        if not path.endswith(('.yaml', '.yml')):
            raise ValueError(f"Invalid file extension (expected .yaml or .yml): {path}")

        try:
            with open(path, 'r', encoding='utf-8') as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML parsing error: {e}")
        except Exception as e:
            raise RuntimeError(f"Unexpected error reading YAML file: {e}")
        return v


load_dotenv()


def get_diagnosis_agent_cfg():
    client = get_client()

    exec_read_only_kubectl_cmd = ExecReadOnlyKubectlCmd(client)
    diagnosis_agent_cfg = BaseAgentCfg(
        prompts_file_path=str(parent_dir / "stratus_diagnosis_agent_prompts.yaml"),
        sync_tools=[submit_tool],
        async_tools=[get_traces, get_services,
                     get_operations, get_metrics,
                     exec_read_only_kubectl_cmd],
    )
    return diagnosis_agent_cfg


def get_client():
    session_id = str(uuid.uuid4())
    transport = SSETransport(
        url=f"{os.environ['MCP_SERVER_URL']}/kubectl_mcp_tools/sse",
        headers={"srearena_ssid": session_id},
    )
    client = Client(transport)
    return client


def get_mitigation_agent_cfg():
    client = get_client()

    exec_read_only_kubectl_cmd = ExecReadOnlyKubectlCmd(client)
    exec_kubectl_cmd_safely = ExecKubectlCmdSafely(client)
    rollback_command = RollbackCommand(client)
    get_previous_rollbackable_cmd = GetPreviousRollbackableCmd(client)
    mitigation_agent_cfg = BaseAgentCfg(
        prompts_file_path=str(parent_dir / "stratus_mitigation_agent_prompts.yaml"),
        sync_tools=[submit_tool, wait_tool],
        async_tools=[get_traces, get_services,
                     get_operations, get_metrics,
                     exec_read_only_kubectl_cmd,
                     exec_kubectl_cmd_safely,
                     rollback_command,
                     get_previous_rollbackable_cmd],
    )
    return mitigation_agent_cfg
