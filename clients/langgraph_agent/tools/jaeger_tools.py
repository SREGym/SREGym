import logging
import os.path
from pathlib import Path
from typing import Annotated, Optional, Union
import os
import sys
from contextlib import AsyncExitStack
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage,SystemMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from clients.langgraph_agent.state import State
from clients.langgraph_agent.tools.text_editing.flake8_utils import flake8, format_flake8_output  # type: ignore
from clients.langgraph_agent.tools.text_editing.windowed_file import (  # type: ignore
    FileNotOpened,
    TextNotFound,
    WindowedFile,
)
from clients.langgraph_agent.tools.text_editing.file_manip import update_file_vars_in_state 

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools.base import ArgsSchema, BaseTool
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from clients.langgraph_agent.llm_backend.init_backend import get_llm_backend_for_tools



USE_HTTP = True  # Set to False to use local server
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
@tool("get_traces", description = "Get Jaeger traces")

async def get_traces(
    service: str,
    last_n_minutes: int,
    state: Annotated[dict, InjectedState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
    """Get Jaeger traces for a given service in the last n minutes."""
    logging.info(f"Getting traces for service {service} in the last {last_n_minutes} minutes")
    
    exit_stack = AsyncExitStack()
    server_name = "observability"
    if USE_HTTP:
        logger.info("Using HTTP, connecting to server.")
        server_url = "http://127.0.0.1:8000/sse"
        http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
        session = await exit_stack.enter_async_context(ClientSession(*http_transport))
    else:
        logger.info("Not using HTTP, booting server locally, not recommended.")
        curr_dir = os.getcwd()
        logger.info(f"current dir: {curr_dir}")
        server_path = f"{curr_dir}/mcp_server/observability_server.py"
        logger.info(f"Connecting to server: {server_name} at path: {server_path}")
        is_python = server_path.endswith(".py")
        is_js = server_path.endswith(".js")
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")
        command = (
            sys.executable if is_python else "node"
        )  # Uses the current Python interpreter from the activated venv
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
            "last_n_minutes": last_n_minutes,
        },
    )
    await exit_stack.aclose()
    traces_summary = _summarize_traces(result)
    logger.info(f"Traces summary: {traces_summary}")
    return Command(
        update = update_file_vars_in_state(
            state,
            ToolMessage(
                content=traces_summary.content[0].text,
                tool_call_id=tool_call_id,
            )
        )
    ) 

def _summarize_traces(traces):
        logger.info("=== _summarize_traces called ===")

        system_prompt = """
        You are a tool for a Site Reliability Engineering team. Currently, the team faces an incident in the cluster and needs to fix it ASAP.
            Your job is to analyze and summarize given microservice traces, given in format of dictionaries.
            Read the given traces. Summarize the traces. Analyze what could be the root cause of the incident.
            Be succinct and concise. Include important traces that reflects the root cause of the incident in format of raw traces as strings, no need to prettify the json.
            DO NOT truncate the traces.

            Return your response in this format:
            SERVICE NAME: <insert service name>
            SUMMARY: <insert summary of traces>

            STRICTLY FOLLOW THIS FORMAT
            
            """
        logger.info(f"raw traces received: {traces}")
        llm = get_llm_backend_for_tools()
        messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=traces.content[0].text),
        ]

        traces_summary =  llm.inference(messages=messages)
        logger.info(f"Traces summary: {traces_summary}")
        return traces_summary


@tool ("get_services", description="Get all services in the cluster")

async def get_services(
     state: Annotated[dict, InjectedState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
)-> Command:
    logger.info(f"calling mcp get_services from langchain get_services")
    exit_stack = AsyncExitStack()
    server_name = "observability"
    if USE_HTTP:
            logger.info("Using HTTP, connecting to server.")
            server_url = "http://127.0.0.1:9953/sse"
            http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
            session = await exit_stack.enter_async_context(ClientSession(*http_transport))
    else:
            logger.info("Not using HTTP, booting server locally, not recommended.")
            curr_dir = os.getcwd()
            logger.info(f"current dir: {curr_dir}")
            server_path = f"{curr_dir}/mcp_server/observability_server.py"
            logger.info(f"Connecting to server: {server_name} at path: {server_path}")
            is_python = server_path.endswith(".py")
            is_js = server_path.endswith(".js")
            if not (is_python or is_js):
                raise ValueError("Server script must be a .py or .js file")
            command = (
                sys.executable if is_python else "node"
            )  # Uses the current Python interpreter from the activated venv
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
    logger.info(f"Result from get_services: {result}")
    services = result.content[0].text
    return Command(
        update = update_file_vars_in_state(
            state,
            ToolMessage(
                content=services,
                tool_call_id=tool_call_id,
            )
        )
    )

@tool ("get_operations" , description="Get all operations in the cluster")

async def get_operations( 
    service: str,
    state: Annotated[dict, InjectedState] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    
) -> Command: 
    logger.info(f"calling mcp get_operations from langchain get_operations with service {service}")
    exit_stack = AsyncExitStack()
    server_name = "observability"
    if USE_HTTP:
            logger.info("Using HTTP, connecting to server.")
            server_url = "http://127.0.0.1:9953/sse"
            http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
            session = await exit_stack.enter_async_context(ClientSession(*http_transport))
    else:
            logger.info("Not using HTTP, booting server locally, not recommended.")
            curr_dir = os.getcwd()
            logger.info(f"current dir: {curr_dir}")
            server_path = f"{curr_dir}/mcp_server/observability_server.py"
            logger.info(f"Connecting to server: {server_name} at path: {server_path}")
            is_python = server_path.endswith(".py")
            is_js = server_path.endswith(".js")
            if not (is_python or is_js):
                raise ValueError("Server script must be a .py or .js file")
            command = (
                sys.executable if is_python else "node"
            )  # Uses the current Python interpreter from the activated venv
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
    operations = result.content[0].text
    logger.info(f"Result from get_operations: {operations}")
    return Command(
        update = update_file_vars_in_state(
            state,
            ToolMessage(
                content=operations,
                tool_call_id=tool_call_id,
            )
        )
    )