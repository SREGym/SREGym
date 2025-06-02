import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import tool
from langchain_core.tools.base import ArgsSchema, BaseTool
from mcp import ClientSession, StdioServerParameters, stdio_client
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class GetTracesInput(BaseModel):
    service: str = Field(description="service name")
    operation: str = Field(description="operation name")
    last_n_minutes: int = Field(description="last n minutes of traces")


class GetTraces(BaseTool):
    name: str = "get_traces"
    description: str = "get traces of last n minutes from jaeger by service and operation"
    args_schema: Optional[ArgsSchema] = GetTracesInput

    def _run(
        self,
        service: str,
        operation: str,
        last_n_minutes: int,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        logger.error("no sync version of tools, exiting.")
        sys.exit(1)

    async def _arun(
        self,
        service: str,
        operation: str,
        last_n_minutes: int,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        logger.info(
            f"calling mcp get_traces from langchain get_traces, with service {service} and operation {operation}"
        )
        curr_dir = os.getcwd()
        logger.info(f"current dir: {curr_dir}")
        server_path = f"{curr_dir}/mcp_server/observability_server.py"
        server_name = "observability"
        exit_stack = AsyncExitStack()
        logger.info(f"Connecting to server: {server_name} at path: {server_path}")
        is_python = server_path.endswith(".py")
        is_js = server_path.endswith(".js")
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = sys.executable if is_python else "node"  # Uses the current Python interpreter from the activated venv
        server_params = StdioServerParameters(command=command, args=[server_path], env=None)

        logging.info(f"Starting server: {server_name} with params: {server_params}")
        stdio_transport = await exit_stack.enter_async_context(stdio_client(server_params))

        stdio, write = stdio_transport
        session = await exit_stack.enter_async_context(ClientSession(stdio, write))

        await session.initialize()
        logger.info(f"Connected to server: {server_name}, adding to session dict")

        logger.info(f"Listing tools for server: {server_name}")
        response = await session.list_tools()
        tools = response.tools
        logger.info("Connected to server with tools, %s", [tool.name for tool in tools])
        result = await session.call_tool(
            "get_traces",
            arguments={
                "service": service,
                "operation": operation,
                "last_n_minutes": last_n_minutes,
            },
        )
        await exit_stack.aclose()
        return result


class GetServices(BaseTool):
    name: str = "get_services"
    description: str = "get services from jaeger"
    args_schema: Optional[ArgsSchema] = None

    def _run(self, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        logger.error("no sync version of tools, exiting.")
        sys.exit(1)

    async def _arun(
        self,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        logger.info(f"calling mcp get_services from langchain get_services")
        curr_dir = os.getcwd()
        logger.info(f"current dir: {curr_dir}")
        server_path = f"{curr_dir}/mcp_server/observability_server.py"
        server_name = "observability"
        exit_stack = AsyncExitStack()
        logger.info(f"Connecting to server: {server_name} at path: {server_path}")
        is_python = server_path.endswith(".py")
        is_js = server_path.endswith(".js")
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = sys.executable if is_python else "node"  # Uses the current Python interpreter from the activated venv
        server_params = StdioServerParameters(command=command, args=[server_path], env=None)

        logging.info(f"Starting server: {server_name} with params: {server_params}")
        stdio_transport = await exit_stack.enter_async_context(stdio_client(server_params))

        stdio, write = stdio_transport
        session = await exit_stack.enter_async_context(ClientSession(stdio, write))

        await session.initialize()
        logger.info(f"Connected to server: {server_name}, adding to session dict")

        logger.info(f"Listing tools for server: {server_name}")
        response = await session.list_tools()
        tools = response.tools
        logger.info("Connected to server with tools, %s", [tool.name for tool in tools])
        result = await session.call_tool("get_services")
        await exit_stack.aclose()
        return result


class GetOperationsInput(BaseModel):
    service: str = Field(description="service name")


class GetOperations(BaseTool):
    name: str = "get_operations"
    description: str = "get operations from jaeger by service"
    args_schema: Optional[ArgsSchema] = GetOperationsInput

    def _run(self, service: str, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        logger.error("no sync version of tools, exiting.")
        sys.exit(1)

    async def _arun(
        self,
        service: str,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        logger.info(f"calling mcp get_operations from langchain get_operations with service {service}")
        curr_dir = os.getcwd()
        logger.info(f"current dir: {curr_dir}")
        server_path = f"{curr_dir}/mcp_server/observability_server.py"
        server_name = "observability"
        exit_stack = AsyncExitStack()
        logger.info(f"Connecting to server: {server_name} at path: {server_path}")
        is_python = server_path.endswith(".py")
        is_js = server_path.endswith(".js")
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = sys.executable if is_python else "node"  # Uses the current Python interpreter from the activated venv
        server_params = StdioServerParameters(command=command, args=[server_path], env=None)

        logging.info(f"Starting server: {server_name} with params: {server_params}")
        stdio_transport = await exit_stack.enter_async_context(stdio_client(server_params))

        stdio, write = stdio_transport
        session = await exit_stack.enter_async_context(ClientSession(stdio, write))

        await session.initialize()
        logger.info(f"Connected to server: {server_name}, adding to session dict")

        logger.info(f"Listing tools for server: {server_name}")
        response = await session.list_tools()
        tools = response.tools
        logger.info("Connected to server with tools, %s", [tool.name for tool in tools])
        result = await session.call_tool(
            "get_operations",
            arguments={"service": service},
        )
        await exit_stack.aclose()
        return result
