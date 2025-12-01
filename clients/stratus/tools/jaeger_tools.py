import logging
from contextlib import AsyncExitStack
from typing import Annotated

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from mcp import ClientSession
from mcp.client.sse import sse_client

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from clients.stratus.llm_backend.init_backend import get_llm_backend_for_tools
from clients.stratus.stratus_utils.truncate_by_token import truncate_to_tokens
from clients.stratus.tools.text_editing.flake8_utils import flake8, format_flake8_output  # type: ignore
from clients.stratus.tools.text_editing.windowed_file import (  # type: ignore
    FileNotOpened,
    TextNotFound,
    WindowedFile,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("all.stratus.tools.jaeger")

langgraph_tool_config = LanggraphToolConfig()

get_traces_docstring = """Get Jaeger traces for a given service in the last n minutes.

    Args:
        service (str): The name of the service for which to retrieve trace data.
        last_n_minutes (int): The time range (in minutes) to look back from the current time.
"""


@tool(description=get_traces_docstring)
async def get_traces(service: str, last_n_minutes: int, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:

    logging.info(f"Getting traces for service {service} in the last {last_n_minutes} minutes")

    exit_stack = AsyncExitStack()
    logger.info("Using HTTP, connecting to server.")
    server_url = langgraph_tool_config.jaeger_mcp_url
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool(
        "get_traces",
        arguments={
            "service": service,
            "last_n_minutes": last_n_minutes,
        },
    )
    await exit_stack.aclose()
    result = result.content[0].text
    # if langgraph_tool_config.use_summaries and len(traces) >= langgraph_tool_config.min_len_to_sum:
    #     logger.info("Using summaries for traces.")
    #     traces = _summarize_traces(traces)
    result = truncate_to_tokens(result)
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=str(result),
                    tool_call_id=tool_call_id,
                ),
            ]
        }
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
    llm = get_llm_backend_for_tools()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=traces),
    ]

    traces_summary = llm.inference(messages=messages)
    logger.info(f"Traces summary: {traces_summary}")
    return traces_summary


def _summarize_operations(operations):
    logger.info("=== _summarize_operations called ===")

    system_prompt = """
        You are a tool for a Site Reliability Engineering team. Currently, the team faces an incident in the cluster and needs to fix it ASAP.
            Your job is to analyze and summarize given microservice operations, given in format of dictionaries.
            Read the given operations. Summarize the operations. Analyze what could be the root cause of the incident.
            Be succinct and concise. 

            Return your response in this format:
            SERVICE NAME: <insert service name>
            SUMMARY: <insert summary of operations>

            STRICTLY FOLLOW THIS FORMAT
            
            """
    llm = get_llm_backend_for_tools()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=operations),
    ]

    operations_summary = llm.inference(messages=messages)
    logger.info(f"Operations summary: {operations_summary}")
    return operations_summary


get_services_docstring = """
Retrieve the list of service names from the Grafana instance.

    Args:

    Returns:
        List[str]: A list of service names available in Grafana.
"""


@tool(description=get_services_docstring)
async def get_services(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:

    logger.info(f"calling mcp get_services from langchain get_services")
    exit_stack = AsyncExitStack()
    logger.info("Using HTTP, connecting to server.")
    server_url = langgraph_tool_config.jaeger_mcp_url
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool("get_services")
    await exit_stack.aclose()
    # services = result.content[0].text
    logger.debug(f"Result from get_services mcp tools: f{result}")
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=result,
                    tool_call_id=tool_call_id,
                ),
            ]
        }
    )


get_operations_docstring = """
Query available operations for a specific service from the Grafana instance.

    Args:
        service (str): The name of the service whose operations should be retrieved.

    Returns:
        List[str]: A list of operation names associated with the specified service.
"""


@tool(description=get_operations_docstring)
async def get_operations(
    service: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:

    logger.info(f"calling mcp get_operations from langchain get_operations with service {service}")
    exit_stack = AsyncExitStack()
    logger.info("Using HTTP, connecting to server.")
    server_url = langgraph_tool_config.jaeger_mcp_url
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool(
        "get_operations",
        arguments={"service": service},
    )
    await exit_stack.aclose()
    # operations = result.content[0].text
    # if langgraph_tool_config.use_summaries and len(operations) >= langgraph_tool_config.min_len_to_sum:
    #     logger.info("Using summaries for operations.")
    #     operations = _summarize_operations(operations)
    return Command(
        update={
            "messages": [
                ToolMessage(content=result, tool_call_id=tool_call_id),
            ]
        }
    )


get_dependency_graph_docstring = """
    Get service dependency graph from Jaeger's native dependencies API.
    Args:
        last_n_minutes (int): The time range (in minutes) to look back from the current time.
    Returns:
        str: JSON object representing the dependency graph.
"""


@tool(description=get_dependency_graph_docstring)
async def get_dependency_graph(
    last_n_minutes: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:

    logger.info(f"calling mcp get_dependency_graph from langchain get_dependency_graph")
    exit_stack = AsyncExitStack()
    logger.info("Using HTTP, connecting to server.")
    server_url = langgraph_tool_config.jaeger_mcp_url
    http_transport = await exit_stack.enter_async_context(sse_client(url=server_url))
    session = await exit_stack.enter_async_context(ClientSession(*http_transport))

    await session.initialize()

    result = await session.call_tool(
        "get_dependency_graph",
        arguments={"last_n_minutes": last_n_minutes},
    )
    await exit_stack.aclose()
    # operations = result.content[0].text
    # if langgraph_tool_config.use_summaries and len(operations) >= langgraph_tool_config.min_len_to_sum:
    #     logger.info("Using summaries for operations.")
    #     operations = _summarize_operations(operations)
    return Command(
        update={
            "messages": [
                ToolMessage(content=result, tool_call_id=tool_call_id),
            ]
        }
    )
