import logging
from typing import Optional

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import tool
from langchain_core.tools.base import ArgsSchema, BaseTool
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@tool("get_traces")
def get_traces(mcp_ctx, service: str, operation: str, last_n_minutes: int) -> str:
    result = mcp_ctx.call_tool(
        "get_traces",
        arguments={
            "service": service,
            "operation": operation,
            "last_n_minutes": last_n_minutes,
        },
    )
    logger.info(
        f"calling mcp get_traces from langchain get_traces, with service {service} and operation {operation}"
    )
    return result


@tool("get_services")
def get_services(mcp_ctx) -> str:
    result = mcp_ctx.call_tool("get_services")
    logger.info(f"calling mcp get_services from langchain get_services")
    return result


@tool("get_operations")
def get_operations(mcp_ctx, service: str) -> str:
    result = mcp_ctx.call_tool("get_operations", arguments={"service": service})
    logger.info(f"calling mcp get_operations from langchain get_operations")
    return result
