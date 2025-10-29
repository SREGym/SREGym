import logging

from fastmcp import Context, FastMCP
from yarl import URL

from clients.stratus.stratus_utils.get_logger import get_logger
from mcp_server.configs.load_all_cfg import kubectl_session_cfg
from mcp_server.kubectl_server_helper.kubectl_tool_set import KubectlToolSet
from mcp_server.kubectl_server_helper.sliding_lru_session_cache import SlidingLRUSessionCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("all.mcp.kubectl_mcp_tools")

sessionCache = SlidingLRUSessionCache(
    max_size=kubectl_session_cfg.session_cache_size, ttl_seconds=kubectl_session_cfg.session_ttl
)

logger = get_logger()

kubectl_mcp = FastMCP("Kubectl MCP Server")
logger.info("Starting Kubectl MCP Server")


def extract_session_id(ctx: Context):
    """
    Use this function to get the session id of the request
    First use custom session id.
    """
    ssid = ctx.request_context.request.headers.get("sregym_ssid")
    if ssid is None:
        str_url = str(ctx.request_context.request.url)
        url = URL(str_url)
        ssid = url.query.get("session_id")
    return ssid


def get_tools(session_id: str) -> KubectlToolSet:
    """
    Get the tools related with session_id. If no
    tools, create a new one for this session.
    """
    tool = sessionCache.get(session_id)
    if tool is not None:
        return tool

    logger.debug(f"Creating a new kubectl tool for session {session_id}.")
    tool = KubectlToolSet(session_id)
    sessionCache[session_id] = tool
    return tool


@kubectl_mcp.tool()
def exec_kubectl_cmd_safely(cmd: str, ctx: Context) -> str:
    """
    Use this function to execute kubectl commands.
    Args:
        cmd: The command you want to execute in a CLI to
        manage a k8s cluster. It should start with "kubectl".
        ctx: If you are an agent, you can safely ignore this
        argument.
    Returns:
        The result of trying to execute cmd.
    """
    ssid = extract_session_id(ctx)
    kubctl_tool = get_tools(ssid)
    logger.debug(f'session {ssid} is using tool "exec_kubectl_cmd_safely"; Command: {cmd}.')
    result = kubctl_tool.cmd_runner.exec_kubectl_cmd_safely(cmd)
    assert isinstance(result, str)
    return result


@kubectl_mcp.tool()
def rollback_command(ctx: Context) -> str:
    """
    Use this function to roll back the last kubectl command
    you successfully executed with the "exec_kubectl_cmd_safely" tool.
    Args:
        ctx: If you are an agent, you can safely ignore this
        argument.
    Returns:
        The result of trying to roll back the last kubectl command.
    """
    ssid = extract_session_id(ctx)
    kubectl_tool = get_tools(ssid)
    logger.debug(f'session {ssid} is using tool "rollback_command".')
    result = kubectl_tool.rollback_tool.rollback()
    assert isinstance(result, str)
    return f"{result}, action_stack: {kubectl_tool.rollback_tool.action_stack}"


@kubectl_mcp.tool()
def get_previous_rollbackable_cmd(ctx: Context) -> str:
    """
    Use this function to get a list of commands you
    previously executed that could be roll-backed.

    Returns:
        Text content that shows a list of commands you
        previously executed that could be roll-backed.
        When you call rollback_command tool multiple times,
        you will roll-back previous commands in the order
        of the returned list.
    """
    ssid = extract_session_id(ctx)
    kubctl_tool = get_tools(ssid)
    logger.debug(f'session {ssid} is using tool "get_previous_rollbackable_cmd".')
    cmds = kubctl_tool.rollback_tool.get_previous_rollbackable_cmds()
    return "\n".join([f"{i + 1}. {cmd}" for i, cmd in enumerate(cmds)])
