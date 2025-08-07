from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, field_validator
from pathlib import Path
import os
import logging
import yaml
from clients.langgraph_agent.tools.jaeger_tools import \
    get_traces, \
    get_services, \
    get_operations

from clients.langgraph_agent.tools.prometheus_tools import get_metrics
from clients.langgraph_agent.tools.submit_tool import submit_tool

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

parent_dir = Path(__file__).resolve().parent


class BaseAgentCfg(BaseModel):
    max_round: int = Field(
        default=20,
        description="maximum times of allowed tool calling ",
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


diagnosis_agent_cfg = BaseAgentCfg(
    prompts_file_path=str(parent_dir / "stratus_diagnosis_agent_prompts.yaml"),
    sync_tools=[submit_tool],
    async_tools=[get_traces, get_services, get_operations, get_metrics],
)
