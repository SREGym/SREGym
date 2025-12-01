import os

from dotenv import load_dotenv

from mcp_server.configs.kubectl_session_cfg import KubectlSessionCfg
from mcp_server.configs.mcp_server_cfg import McpServerCfg

load_dotenv()


def str_to_bool(s: str) -> bool:
    """
    Convert a string to a boolean value.

    True values: 'true', '1', 'yes', 'y', 'on'
    False values: 'false', '0', 'no', 'n', 'off'

    Raises:
        ValueError: if the string does not represent a boolean.

    Args:
        s (str): The string to convert.

    Returns:
        bool: The converted boolean value.
    """
    if not isinstance(s, str):
        raise TypeError("Input must be a string.")

    true_values = {"true", "1", "yes", "y", "on"}
    false_values = {"false", "0", "no", "n", "off"}

    s_lower = s.strip().lower()
    if s_lower in true_values:
        return True
    elif s_lower in false_values:
        return False
    else:
        raise ValueError(f"Invalid literal for boolean: '{s}'")


mcp_server_cfg = McpServerCfg(
    mcp_server_port=int(os.getenv("MCP_SERVER_PORT", "9954")),
    expose_server=str_to_bool(os.getenv("EXPOSE_SERVER", "False")),
)

kubectl_session_cfg = KubectlSessionCfg(
    session_cache_size=int(os.getenv("SESSION_CACHE_SIZE", "10000")), session_ttl=int(os.getenv("SESSION_TTL", "600"))
)
