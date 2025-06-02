import logging
from typing import Any

import httpx
import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base
from pydantic import AnyUrl

logger = logging.getLogger("Example MCP Server")
logger.info("Starting Example MCP Server")

mcp = FastMCP("Example MCP Server")


@mcp.resource("resource://example-txt")
def get_example_txt() -> Any:
    logger.debug("get_example_txt called")
    with open("./mcp_server/example.txt", "r") as f:
        return f.read()


@mcp.resource("resource://example-txt/{string}")
def get_example_txt_with_str(string: str) -> Any:
    logger.debug("get_example_txt called")
    with open("./mcp_server/example.txt", "r") as f:
        return f"inserted str: {string}, example txt content: {f.read()}"


@mcp.tool()
def surround(character: str, main_body: str) -> str:
    logger.debug("surround called")
    return f"{character}{main_body}{character}"


@mcp.prompt(name="summarize_example_text")
def summarize_example_text(text: str) -> list[base.Message]:
    return [
        base.UserMessage("Please summarize this text"),
        base.UserMessage(text),
    ]


if __name__ == "__main__":
    mcp.run()
